__author__ = 'charles'
import sqlite3
import os
import hashlib
import time
import fnmatch
import pickle
import logging

from utils import hashfile

from watchdog.events import FileSystemEventHandler
from watchdog.utils.dirsnapshot import DirectorySnapshot,DirectorySnapshotDiff


class SqlSnapshot(object):

    def __init__(self, basepath):
        self.db = 'data/pydio.sqlite'
        self.basepath = basepath
        self._stat_snapshot = {}
        self._inode_to_path = {}
        self.is_recursive = True
        self.load_from_db()

    def load_from_db(self):

        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        for row in c.execute("SELECT node_path,stat_result FROM ajxp_index WHERE stat_result NOT NULL"):
            stat = pickle.loads(row['stat_result'])
            path = self.basepath + row['node_path']
            self._stat_snapshot[path] = stat
            self._inode_to_path[stat.st_ino] = path
        c.close()

    def __sub__(self, previous_dirsnap):
        """Allow subtracting a DirectorySnapshot object instance from
        another.

        :returns:
            A :class:`DirectorySnapshotDiff` object.
        """
        return DirectorySnapshotDiff(previous_dirsnap, self)

    @property
    def stat_snapshot(self):
        """
        Returns a dictionary of stat information with file paths being keys.
        """
        return self._stat_snapshot


    def stat_info(self, path):
        """
        Returns a stat information object for the specified path from
        the snapshot.

        :param path:
            The path for which stat information should be obtained
            from a snapshot.
        """
        return self._stat_snapshot[path]


    def path_for_inode(self, inode):
        """
        Determines the path that an inode represents in a snapshot.

        :param inode:
            inode number.
        """
        return self._inode_to_path[inode]


    def stat_info_for_inode(self, inode):
        """
        Determines stat information for a given inode.

        :param inode:
            inode number.
        """
        return self.stat_info(self.path_for_inode(inode))


    @property
    def paths(self):
        """
        List of file/directory paths in the snapshot.
        """
        return set(self._stat_snapshot)


class LocalDbHandler():

    def __init__(self, base=''):
        self.base = base
        self.db = "data/pydio.sqlite"
        if not os.path.exists(self.db):
            self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.db)

        cursor = conn.cursor()
        with open('create.sql', 'r') as inserts:
            for statement in inserts:
                cursor.execute(statement)
        conn.close()

    def find_node_by_id(self, node_path):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        for row in c.execute("SELECT node_id FROM ajxp_index WHERE node_path LIKE ?", (node_path.decode('utf-8'))):
            return row['node_id']
        c.close()
        return False

    def get_node_md5(self, node_path):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        for row in c.execute("SELECT md5 FROM ajxp_index WHERE node_path LIKE ?", (node_path.decode('utf-8'))):
            return row['md5']
        c.close()
        return hashfile(self.base + node_path, hashlib.md5())


    def compare_raw_pathes(self, row1, row2):
        if row1['source'] != 'NULL':
            cmp1 = row1['source']
        else:
            cmp1 = row1['target']
        if row2['source'] != 'NULL':
            cmp2 = row2['source']
        else:
            cmp2 = row2['target']
        return cmp1 == cmp2

    def get_local_changes(self, seq_id, accumulator):

        logging.debug("Local sequence " + str(seq_id))
        last = seq_id
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        previous_node_id = -1
        previous_row = None
        orders = dict()
        orders['path'] = 0
        orders['content'] = 1
        orders['delete'] = 3
        orders['create'] = 3
        for row in c.execute("SELECT * FROM ajxp_changes LEFT JOIN ajxp_index "
                             "ON ajxp_changes.node_id = ajxp_index.node_id "
                             "WHERE seq > ? ORDER BY ajxp_changes.node_id, seq ASC", (seq_id,)):
            drow = dict(row)
            drow['node'] = dict()
            for att in ('mtime', 'md5', 'bytesize', 'node_path',):
                drow['node'][att] = row[att]
                drow.pop(att, None)
            if drow['node_id'] == previous_node_id and self.compare_raw_pathes(drow, previous_row):
                previous_row['target'] = drow['target']
                previous_row['seq'] = drow['seq']
                #if orders[drow['type']] > orders[previous_row['type']]:
                if (drow['type'] == 'path' or drow['type'] == 'content'):
                    if previous_row['type'] == 'delete':
                        previous_row['type'] = drow['type']
                    elif previous_row['type'] == 'create':
                        previous_row['type'] = 'create'
                    else:
                        previous_row['type'] = drow['type']
                elif drow['type'] == 'create':
                    previous_row['type'] = 'create'
                else:
                    previous_row['type'] = drow['type']

            else:
                if previous_row is not None and (previous_row['source'] != previous_row['target'] or previous_row['type'] == 'content'):
                    previous_row['location'] = 'local'
                    accumulator.append(previous_row)
                previous_row = drow
                previous_node_id = drow['node_id']
            last = max(row['seq'], last)

        if previous_row is not None and (previous_row['source'] != previous_row['target'] or previous_row['type'] == 'content'):
            previous_row['location'] = 'local'
            accumulator.append(previous_row)

        #refilter: create + delete or delete + create must be ignored
        for row in accumulator:
            print str(row['seq']) + '-' + row['type'] + '-' + row['source'] + '-' + row['target']

        conn.close()
        return last


class SqlEventHandler(FileSystemEventHandler):

    def __init__(self, basepath, includes, excludes):
        super(SqlEventHandler, self).__init__()
        self.base = basepath
        self.includes = includes
        self.excludes = excludes
        db_handler = LocalDbHandler(basepath)
        self.db = db_handler.db

    def remove_prefix(self, text):
        return text[len(self.base):] if text.startswith(self.base) else text

    def included(self, event, base=None):
        if not base:
            base = os.path.basename(event.src_path)
        for i in self.includes:
            if not fnmatch.fnmatch(base, i):
                return False
        for e in self.excludes:
            if fnmatch.fnmatch(base, e):
                return False
        return True

    def on_moved(self, event):
        logging.debug("Event: move noticed: " + event.event_type + " on file " + event.src_path + " at " + time.asctime())
        if not self.included(event):
            return
        conn = sqlite3.connect(self.db)
        t = (
            self.remove_prefix(event.dest_path.decode('utf-8')),
            self.remove_prefix(event.src_path.decode('utf-8')),
        )
        conn.execute("UPDATE ajxp_index SET node_path=? WHERE node_path=?", t)
        conn.commit()
        conn.close()

    def on_created(self, event):
        logging.debug("Event: creation noticed: " + event.event_type +
                         " on file " + event.src_path + " at " + time.asctime())
        if not self.included(event):
            return

        search_key = self.remove_prefix(event.src_path.decode('utf-8'))
        if event.is_directory:
            hash_key = 'directory'
        else:
            hash_key = hashfile(open(event.src_path, 'rb'), hashlib.md5())

        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        node_id = None
        for row in c.execute("SELECT node_id FROM ajxp_index WHERE node_path=?", (search_key,)):
            node_id = row['node_id']
            break
        c.close()
        if not node_id:
            t = (
                search_key,
                os.path.getsize(event.src_path),
                hash_key,
                os.path.getmtime(event.src_path),
                pickle.dumps(os.stat(event.src_path))
            )
            conn.execute("INSERT INTO ajxp_index (node_path,bytesize,md5,mtime,stat_result) VALUES (?,?,?,?,?)", t)
        else:
            t = (
                os.path.getsize(event.src_path),
                hash_key,
                os.path.getmtime(event.src_path),
                pickle.dumps(os.stat(event.src_path)),
                search_key
            )
            conn.execute("UPDATE ajxp_index SET bytesize=?, md5=?, mtime=?, stat_result=? WHERE node_path=?", t)
        conn.commit()
        conn.close()

    def on_deleted(self, event):
        logging.debug("Event: deletion noticed: " + event.event_type +
                         " on file " + event.src_path + " at " + time.asctime())
        if not self.included(event):
            return

        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM ajxp_index WHERE node_path LIKE ?", (self.remove_prefix(event.src_path.decode('utf-8')) + '%',))
        conn.commit()
        conn.close()

    def on_modified(self, event):
        super(SqlEventHandler, self).on_modified(event)
        if not self.included(event):
            return

        if event.is_directory:
            files_in_dir = [event.src_path+"/"+f for f in os.listdir(event.src_path)]
            if len(files_in_dir) > 0:
                modified_filename = max(files_in_dir, key=os.path.getmtime)
            else:
                return
            if os.path.isfile(modified_filename) and self.included(event=None, base=modified_filename):
                logging.debug("Event: modified file : %s" % modified_filename)
                conn = sqlite3.connect(self.db)
                size = os.path.getsize(modified_filename)
                the_hash = hashfile(open(modified_filename, 'rb'), hashlib.md5())
                mtime = os.path.getmtime(modified_filename)
                search_path = self.remove_prefix(modified_filename.decode('utf-8'))
                stat_result = pickle.dumps(os.stat(modified_filename))
                t = (size, the_hash, mtime, search_path, mtime, the_hash, stat_result)
                conn.execute("UPDATE ajxp_index SET bytesize=?, md5=?, mtime=?, stat_result=? WHERE node_path=? AND bytesize!=? AND md5!=?", t)
                conn.commit()
                conn.close()
        else:
            modified_filename = event.src_path
            logging.debug("Event: modified file : %s" % self.remove_prefix(modified_filename))
            conn = sqlite3.connect(self.db)
            size = os.path.getsize(modified_filename)
            the_hash = hashfile(open(modified_filename, 'rb'), hashlib.md5())
            mtime = os.path.getmtime(modified_filename)
            search_path = self.remove_prefix(modified_filename.decode('utf-8'))
            stat_result = pickle.dumps(os.stat(modified_filename))
            t = (size, the_hash, mtime, search_path, mtime, the_hash, stat_result)
            conn.execute("UPDATE ajxp_index SET bytesize=?, md5=?, mtime=?, stat_result=? WHERE node_path=? AND bytesize!=? AND md5!=?", t)
            conn.commit()
            conn.close()