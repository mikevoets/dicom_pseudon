import pydicom
from pydicom.errors import InvalidDicomError
from pydicom.tag import Tag
from pydicom.dataelem import DataElement
from functools import partial
import shutil
import json
import argparse
import os
import csv
import logging
import re
import sqlite3

TABLE_EXISTS = 'SELECT name FROM sqlite_master WHERE name=?'

TABLE_NAME = 'accession_numbers'
CREATE_TABLE = 'CREATE TABLE %s (id INTEGER PRIMARY KEY AUTOINCREMENT, original, serial, UNIQUE(original))' % TABLE_NAME
INSERT = 'INSERT OR IGNORE INTO %s (original) VALUES (?)' % TABLE_NAME
UPDATE = 'UPDATE %s SET serial = ? WHERE original = ?' % TABLE_NAME
GET = 'SELECT serial FROM %s WHERE original = ?' % TABLE_NAME
SEARCH = 'SELECT original FROM %s WHERE original LIKE ?' % TABLE_NAME

DE_IDENTIFICATION_METHOD = 'Pseudonymized by The Cancer Registry of Norway'

ALLOWED_FILE_META = {  # Attributes taken from https://github.com/dicom/ruby-dicom
  (0x2, 0x0): 1,  # File Meta Information Group Length
  (0x2, 0x1): 1,  # Version
  (0x2, 0x2): 1,  # Media Storage SOP Class UID
  (0x2, 0x3): 1,  # Media Storage SOP Instance UID
  (0x2, 0x10): 1,  # Transfer Syntax UID
  (0x2, 0x12): 1,  # Implementation Class UID
  (0x2, 0x13): 1  # Implementation Version Name
}

ACCESSION_NUMBER = (0x8, 0x50)
SERIES_DESCR = (0x8, 0x103E)
MODALITY = (0x8, 0x60)
BURNT_IN = (0x28, 0x301)
IMAGE_TYPE = (0x8, 0x8)
MANUFACTURER = (0x8, 0x70)
MANUFACTURER_MODEL_NAME = (0x8, 0x1090)

logger = logging.getLogger('dicom_pseudon')
logger.setLevel(logging.INFO)

class Index(object):

    def __init__(self, filename):
        self.db = sqlite3.connect(filename)
        self.cursor = self.db.cursor()

    def close(self):
        self.db.close()

    def table_exists(self, table_name):
        self.cursor.execute(TABLE_EXISTS, (table_name,))
        results = self.cursor.fetchall()
        return len(results) > 0

    def get(self, original):
        if not self.table_exists(TABLE_NAME):
            return None

        self.cursor.execute(GET, (original,))
        results = self.cursor.fetchall()
        if len(results):
            return results[0][0]

    def search(self, original):
        if not self.table_exists(TABLE_NAME):
            return None

        self.cursor.execute(SEARCH, (original,))
        results = self.cursor.fetchall()
        if len(results):
            return results[0][0]

    def insert(self, original):
        if not self.table_exists(TABLE_NAME):
            with self.db as db:
                db.execute(CREATE_TABLE)

        with self.db as db:
            db.execute(INSERT, (original,))

    def update(self, original, serial):
        with self.db as db:
            db.execute(UPDATE, (serial, original,))


class DicomPseudon(object):
    def __init__(self, white_list_file, **kwargs):
        self.white_list_file = white_list_file
        self.index_file = kwargs.get('index_file', 'index.db')
        self.quarantine = kwargs.get('quarantine', 'quarantine')
        self.log_file = kwargs.get('log_file', 'dicom_pseudon.log')
        self.modalities = [string.lower() for string in kwargs.get('modalities', ['mr', 'ct'])]

        try:
            with open(self.white_list_file, 'r') as handle:
                content = json.load(handle)
                self.white_list = self.parse_white_list(content)
        except IOError:
            raise Exception('Could not open white list file.')

        self.index = Index(self.index_file)

        logger.handlers = []
        if not self.log_file:
            self.log = logging.StreamHandler()
        else:
            self.log = logging.FileHandler(self.log_file)

        self.log.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        self.log.setFormatter(formatter)
        logger.addHandler(self.log)

    def close_all(self):
        if self.log_file:
            self.log.flush()
            self.log.close()
        self.index.close()

    @staticmethod
    def destination(source, dest, root):
        if dest.startswith(root):
            raise Exception('Destination directory cannot be inside or equal to source directory')
        if not source.startswith(root):
            raise Exception('The file to be moved must be in the root directory')
        return os.path.normpath(dest)

    def quarantine_file(self, filepath, ident_dir, reason):
        full_quarantine_dir = self.destination(filepath, self.quarantine, ident_dir)
        if not os.path.exists(full_quarantine_dir):
            os.makedirs(full_quarantine_dir)
        quarantine_name = os.path.join(full_quarantine_dir, os.path.basename(filepath))
        logger.info('%s will be moved to quarantine directory due to: %s' % (filepath, reason))
        shutil.copyfile(filepath, quarantine_name)

    # Checks (from https://wiki.cancerimagingarchive.net/download/attachments/
    # 3539047/pixel-checker-filter.script?version=1&modificationDate=1333114118541&api=v2):
    # - ImageType to ensure it does not contain the word SAVE to avoid screen saves/captures
    # - Manufacturer to ensure it's not NAI, http://www.naitechproducts.com/dicombox.html
    # - Manufacturer to ensure it's not PACSGEAR, http://www.pacsgear.com/
    # - Series Description to ensure it does not contain the word SAVE to avoid screen saves/captures
    # - Manufacturer to ensure it's not NAI, http://www.naitechproducts.com/dicombox.html
    # - If BurnedInAnnotation contains YES
    def check_quarantine(self, ds):
        if SERIES_DESCR in ds and ds[SERIES_DESCR].value is not None:
            series_desc = ds[SERIES_DESCR].value.strip().lower()
            if 'patient protocol' in series_desc:
                return True, 'patient protocol'
            elif 'save' in series_desc:
                return True, 'Likely screen capture'

        if MODALITY in ds:
            modality = ds[MODALITY]
            if modality.VM == 1:
                modality = [modality.value]
            for m in modality:
                if m is None or not m.lower() in self.modalities:
                    return True, 'modality not allowed'

        if MODALITY not in ds:
            return True, 'Modality missing'

        if BURNT_IN in ds and ds[BURNT_IN].value is not None:
            burnt_in = ds[BURNT_IN].value
            if burnt_in.strip().lower() in ['yes', 'y']:
                return True, 'burnt-in data'

        if IMAGE_TYPE in ds:
            image_type = ds[IMAGE_TYPE]
            if image_type.VM == 1:
                image_type = [image_type.value]
            for i in image_type:
                if i is not None and 'save' in i.strip().lower():
                    return True, 'Likely screen capture'

        if MANUFACTURER in ds:
            manufacturer = ds[MANUFACTURER].value.strip().lower()
            if 'north american imaging, inc' in manufacturer or 'pacsgear' in manufacturer:
                return True, 'Manufacturer is suspect'

        if MANUFACTURER_MODEL_NAME in ds:
            model_name = ds[MANUFACTURER_MODEL_NAME].value.strip().lower()
            if 'the dicom box' in model_name:
                return True, 'Manufacturer model name is suspect'

        return False, ''

    @staticmethod
    def parse_white_list(h):
        value = {}
        for tag in h.keys():
            a, b = tag.split(',')
            t = (int(a, 16), int(b, 16))
            value[t] = [re.sub(' +', ' ', re.sub('[-_,.]', '', x.strip().lower())) for x in h[tag]]
        return value

    def white_list_handler(self, e):
        value = self.white_list.get((e.tag.group, e.tag.element), None)
        if value:
            if not '*' in value \
                    and not re.sub(' +', ' ', re.sub('[-_,.]', '', str(e.value).strip().lower())) in value:
                logger.info('%s not in white list for %s' % (e.value, e.name))
                return False
            return True
        return False

    def clean(self, ds, e):
        # Skip accession number, it will be replaced after cleaning attributes
        if e.tag == ACCESSION_NUMBER:
            return True

        # Following metadata are necessary to not break DICOM
        if ALLOWED_FILE_META.get((e.tag.group, e.tag.element), None):
            return True

        white_listed = self.white_list_handler(e)

        if not white_listed:
            del ds[e.tag]

        # Tell our caller if we cleaned this element
        return white_listed

    def pseudonymize(self, ds):
        accession_num = ds[ACCESSION_NUMBER].value
        serial_num = self.index.get(accession_num)

        if serial_num is None:
            raise ValueError('No serial number for accession number %s' % (accession_num,))

        ds.walk(partial(self.clean))

        return ds, serial_num

    def walk_dicoms(self, ident_dir, quarantine=False):
        for root, _, files in os.walk(ident_dir):
            for filename in files:
                if filename.startswith('.'):
                    continue
                source_path = os.path.join(root, filename)
                try:
                    yield pydicom.read_file(source_path), source_path, filename
                except IOError:
                    logger.error('Error reading file %s' % source_path)
                    self.close_all()
                    return False
                except InvalidDicomError:  # DICOM formatting error
                    if quarantine:
                        self.quarantine_file(source_path, ident_dir, 'Could not read DICOM file.')
                    continue

    def create_index(self, ident_dir, links_file, delimiter=',', skip_first_line=False):
        # Save accession numbers to virtual search table
        for ds, *_ in self.walk_dicoms(ident_dir):
            self.index.insert(ds.AccessionNumber)

        # Keep track of potential duplicates in links file
        invitation_num_set = set()

        with open(links_file, 'r') as f:
            if skip_first_line is True:
                next(f, None)
            reader = csv.reader(f, delimiter=delimiter)

            for line in reader:
                invitation_num, serial_num = line

                if invitation_num in invitation_num_set:
                    logger.warning('Invitation number %s appears in links file multiple times' % invitation_num)
                    continue

                invitation_num_set.add(invitation_num)
                accession_num = self.index.search('%' + invitation_num + '%')

                if accession_num is None:
                    logger.warning('Could not find accession number for invitation number %s' % invitation_num)
                    continue
                self.index.update(accession_num, serial_num)

    def run(self, ident_dir, clean_dir):
        for ds, source_path, filename in self.walk_dicoms(ident_dir, True):
            move, reason = self.check_quarantine(ds)

            if move:
                self.quarantine_file(source_path, ident_dir, reason)
                continue

            try:
                ds, serial_num = self.pseudonymize(ds)
            except ValueError as e:
                self.quarantine_file(source_path, ident_dir,
                                     'Error running pseudonymize function. Error was: %s' % e)
                continue

            rel_destination_dir = os.path.join(clean_dir, serial_num)
            destination_dir = self.destination(source_path, rel_destination_dir, ident_dir)
            if not os.path.exists(destination_dir):
                os.makedirs(destination_dir)
            # Set Accession Number to serial number from links file
            ds[ACCESSION_NUMBER].value = serial_num

            # Set Patient Identity Removed to YES
            t = Tag((0x12, 0x62))
            ds[t] = DataElement(t, 'CS', 'YES')

            # Set the De-identification method
            t = Tag((0x12, 0x63))
            ds[t] = DataElement(t, 'LO', DE_IDENTIFICATION_METHOD)

            clean_name = os.path.join(destination_dir, filename)
            try:
                ds.save_as(clean_name)
            except IOError:
                logger.error('Error writing file %s' % clean_name)
                self.close_all()
                return False

        self.close_all()
        return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(dest='ident_dir', type=str)
    parser.add_argument(dest='clean_dir', type=str)
    parser.add_argument(dest='links_file', type=str, help='Path to links csv or xslx file')
    parser.add_argument(dest='white_list_file', type=str, help='White list json file')
    parser.add_argument('-d', '--delimiter', type=str, default=',',
                        help='Delimiter for values in links file. Defaults to ,')
    parser.add_argument('-s', '--skip_first_line', action='store_true', default=False,
                        help='Skip first line in links file. Should be set if first line is a header. Defaults to false')
    parser.add_argument('-q', '--quarantine', type=str, default='quarantine',
                        help='Quarantine directory. Defaults to ./quarantine')
    parser.add_argument('-i', '--index_file', type=str, default='index.db',
                        help='Name of sqlite index file. Default to index.db')
    parser.add_argument('-m', '--modalities', type=str, nargs='+', default=['mr', 'ct'],
                        help='Comma separated list of allowed modalities. Defaults to mr,ct')
    parser.add_argument('-l', '--log_file', type=str, default=None,
                        help='Name of file to log messages to. Defaults to console')
    args = parser.parse_args()
    i_dir = args.ident_dir
    c_dir = args.clean_dir
    w_file = args.while_list_file
    l_file = args.links_file
    l_file_delim = args.delimiter
    l_file_skip_line = args.skip_first_line
    del args.ident_dir
    del args.clean_dir
    del args.white_list_file
    del args.links_file
    del args.delimiter
    del args.skip_first_line
    da = DicomPseudon(w_file, **vars(args))
    da.create_index(ident_dir, l_file, l_file_delim, l_file_skip_line)
    da.run(i_dir, c_dir)
