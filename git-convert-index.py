#!/usr/bin/env python

# Usage: python git-convert-index.py
# Command line options (They all work on the v2/v3 index file)
# The -h option shows the header of the index file
# The -i options shows all index entries in the file. (git ls-files --debug
#   format)
# The -c options shows the cache-tree data (test-dump-cache-tree format
# The -u options shows all data that was in the REUC Extension

# Read the index format with git-read-index-v5.py
# read-index-v5 outputs the same format as git ls-files


import hashlib
import binascii
import struct
import os.path
import sys
import python.lib.indexlib as indexlib
from collections import defaultdict


class Reader():
    def __init__(self):
        self._sha1 = hashlib.sha1()
        self._f = open(".git/index", "rb")

    def read(self, n):
        data = self._f.read(n)
        self._sha1.update(data)
        return data

    def read_without_updating_sha1(self, n):
        return self._f.read(n)

    def tell(self):
        return self._f.tell()

    def updateSha1(self, data):
        self._sha1.update(data)

    def getSha1(self):
        return self._sha1

class SHAError(Exception):
    pass


HEADER_SIZE = 24

HEADER_STRUCT = struct.Struct("!4sII")
HEADER_V5_STRUCT = struct.Struct("!4sIIII")

SIZE_STRUCT = struct.Struct("!I")

STAT_DATA_STRUCT = struct.Struct("!IIIIIIIIII 20sh")

XTFLAGS_STRUCT = struct.Struct("!h")

CRC_STRUCT = struct.Struct("!I")

DIRECTORY_DATA_STRUCT = struct.Struct("!HIIIIII 20s")

STAT_DATA_CRC_STRUCT = struct.Struct("!IIIIIIII")

FILE_DATA_STRUCT = struct.Struct("!HHIII 20s")

OFFSET_STRUCT = struct.Struct("!I")

class Header:
    def __init__(self, signature, version, nrofentries):
        self.signature = signature
        self.version = version
        self.nrofentries = nrofentries


class IndexEntry:
    def __init__(self, ctimesec, ctimensec, mtimesec, mtimensec, dev, ino,
            mode, uid, gid, filesize, sha1, flags, pathname, filename,
            xtflags = None):
        self.ctimesec  = ctimesec
        self.ctimensec = ctimensec
        self.mtimesec  = mtimesec
        self.mtimensec = mtimensec
        self.dev       = dev
        self.ino       = ino
        self.mode      = mode
        self.uid       = uid
        self.gid       = gid
        self.filesize  = filesize
        self.sha1      = sha1
        self.flags     = flags
        self.pathname  = pathname
        self.filename  = filename
        self.xtflags   = xtflags


class TreeExtensionData:
    def __init__(self, path, entry_count, subtrees, sha1 = "invalid"):
        self.path        = path
        self.entry_count = entry_count
        self.subtrees    = subtrees
        self.sha1        = sha1


class ReucExtensionData:
    def __init__(self, path, entry_mode0, entry_mode1, entry_mode2, obj_names0,
            obj_names1, obj_names2):
        self.path        = path
        self.entry_mode0 = entry_mode0
        self.entry_mode1 = entry_mode1
        self.entry_mode2 = entry_mode2
        self.obj_names0  = obj_names0
        self.obj_names1  = obj_names1
        self.obj_names2  = obj_names2


class DirEntry:
    def __init__(self, nfiles = 0, flags = 0, cr = 0, ncr = 0, nsubtrees = 0,
            nentries = 0, objname = 20 * '\0'):
        self.nfiles = nfiles
        self.flags = flags
        self.cr = cr
        self.ncr = ncr
        self.nsubtrees = nsubtrees
        self.nentries = nentries
        self.objname = objname


def write_calc_crc(fw, data, partialcrc=0):
    fw.write(data)
    crc = calculate_crc(data, partialcrc)
    return crc


def calculate_crc(data, partialcrc=0):
    return binascii.crc32(data, partialcrc) & 0xffffffff


def read_name(r, delimiter):
    string = ""
    byte = r.read(1)
    readbytes = 1
    while byte != delimiter:
        string = string + byte
        byte = r.read(1)
        readbytes += 1
    return string, readbytes


def read_header(r):
    (signature, version, nrofentries) = HEADER_STRUCT.unpack(
            r.read(HEADER_STRUCT.size))
    return Header(signature, version, nrofentries)


def read_entry(r, header):
    (ctimesec, ctimensec, mtimesec, mtimensec, dev, ino, mode, uid, gid,
            filesize, sha1, flags) = STAT_DATA_STRUCT.unpack(
                    r.read(STAT_DATA_STRUCT.size))

    if header.version == 3:
        xtflags = XTFLAGS_STRUCT.unpack(r.read(XTFLAGS_STRUCT.size))

    (name, readbytes) = read_name(r, '\0')

    pathname = os.path.dirname(name)
    filename = os.path.basename(name)

    entry = IndexEntry(ctimesec, ctimensec, mtimesec, mtimensec, dev, ino,
            mode, uid, gid, filesize, sha1, flags, pathname, filename)

    if (header.version == 3):
        entry.xtflags = xtflags

    if header.version == 2:
        j = 8 - (readbytes + 5) % 8
    else:
        j = 8 - (readbytes + 1) % 8

    # Just throw the padding away.
    r.read(j - 1)

    return entry


def read_index_entries(r, header):
    indexentries = []
    conflictedentries = defaultdict(list)
    paths = set()
    files = list()
    # Read index entries
    for i in xrange(header.nrofentries):
        entry = read_entry(r, header)

        paths.add(entry.pathname)
        files.append(entry.filename)

        stage = (entry.flags & 0b0011000000000000) / 0b001000000000000

        if stage == 0:      # Not conflicted
            indexentries.append(entry)
        else:                   # Conflicted
            if stage == 1:
                # Write the stage 1 entry to the main index, to avoid
                # rewriting the whole index once the conflict is resolved
                indexentries.append(entry)
            conflictedentries[entry.pathname].append(entry)

    return indexentries, conflictedentries, paths, files


def read_tree_extensiondata(r):
    extensionsize = r.read(4)
    read = 0
    subtreenr = [0]
    subtree = [""]
    listsize = 0
    extensiondata = dict()
    while read < int(SIZE_STRUCT.unpack(extensionsize)[0]):
        (path, readbytes) = read_name(r, '\0')
        read += readbytes

        while listsize >= 0 and subtreenr[listsize] == 0:
            subtreenr.pop()
            subtree.pop()
            listsize -= 1

        fpath = ""
        if listsize > 0:
            for p in subtree:
                if p != "":
                    fpath += p + "/"
            subtreenr[listsize] = subtreenr[listsize] - 1
        fpath += path + "/"

        (entry_count, readbytes) = read_name(r, " ")
        read += readbytes

        (subtrees, readbytes) = read_name(r, "\n")
        read += readbytes

        subtreenr.append(int(subtrees))
        subtree.append(path)
        listsize += 1

        if entry_count != "-1":
            sha1 = binascii.hexlify(r.read(20))
            read += 20
        else:
            sha1 = "invalid"

        extensiondata[fpath] = TreeExtensionData(fpath, entry_count, subtrees,
                sha1)

    return extensiondata


def read_reuc_extension_entry(r):
    (path, readbytes) = read_name(r, '\0')
    read = readbytes

    entry_mode = list()
    i = 0
    while i < 3:
        (mode, readbytes) = read_name(r, '\0')
        read += readbytes
        i += 1

        entry_mode.append(int(mode, 8))

    obj_names = list()
    for i in xrange(3):
        if entry_mode[i] != 0:
            obj_names.append(r.read(20))
            read += 20
        else:
            obj_names.append("")

    return ReucExtensionData(path, entry_mode[0], entry_mode[1],
            entry_mode[2], obj_names[0], obj_names[1], obj_names[2]), read


def read_reuc_extensiondata(r):
    extensionsize = r.read(4)

    read = 0
    extensiondata = defaultdict(list)
    while read < int(SIZE_STRUCT.unpack(extensionsize)[0]):
        (entry, readbytes) = read_reuc_extension_entry(r)
        read += readbytes
        extensiondata["/".join(entry.path.split("/"))[:-1]].append(entry)

    return extensiondata


def print_header(header):
    print indexlib.HEADER_FORMAT % {"signature": header.signature,
            "version": header.version, "nrofentries": header.nrofentries}


def print_indexentries(indexentries):
    for entry in indexentries:
        if entry.pathname != "":
            print entry.pathname + "/" + entry.filename
        else:
            print entry.filename
        print indexlib.ENTRIES_FORMAT % {"ctimesec": entry.ctimesec,
                "ctimensec": entry.ctimensec, "mtimesec": entry.mtimesec,
                "mtimensec": entry.mtimensec, "dev": entry.dev,
                "ino": entry.ino, "uid": entry.uid, "gid": entry.gid,
                "filesize": entry.filesize} + "%x" % entry.flags


def print_extensiondata(extensiondata):
    for entry in sorted(extensiondata.itervalues()):
        dictentry = {"sha1": entry.sha1, "path": entry.path,
                "entry_count": entry.entry_count, "subtrees": entry.subtrees}
        try:
            print indexlib.EXTENSION_FORMAT % dictentry
        except KeyError:
            print indexlib.EXTENSION_FORMAT_WITHOUT_SHA % dictentry


def print_reucextensiondata(extensiondata):
    if extensiondata:
        for (path, data) in extensiondata.iteritems():
            for e in data:
                print indexlib.REUCEXTENSION_FORMAT % {"path": e.path,
                        "entry_mode0": e.entry_mode0, "entry_mode1":
                        e.entry_mode1, "entry_mode2": e.entry_mode2}
                print ("Objectnames 1: " + binascii.hexlify(e.obj_names0) +
                        " Objectnames 2: " + binascii.hexlify(e.obj_names1) +
                        " Objectnames 3: " + binascii.hexlify(e.obj_names2))


def write_header(fw, header, paths, files):
    crc = write_calc_crc(fw, HEADER_V5_STRUCT.pack(header.signature, 5,
        len(paths), len(files), 0))
    fw.write(CRC_STRUCT.pack(crc))


def write_fake_dir_offsets(fw, paths):
    for p in paths:
        fw.write(OFFSET_STRUCT.pack(0))


def write_directories(fw, paths):
    diroffsets = list()
    dirwritedataoffsets = dict()
    for p in sorted(paths):
        diroffsets.append(fw.tell())

        # pathname
        if p == "":
            fw.write("\0")
        else:
            fw.write(p + "/\0")

        dirwritedataoffsets[p] = fw.tell()

        # flags, foffset, cr, ncr, nsubtrees, nfiles, nentries, objname, dircrc
        # All this fields will be filled out when the rest of the index
        # is written
        # CRC will be calculated when data is filled in
        fw.write(DIRECTORY_DATA_STRUCT.pack(0, 0, 0, 0, 0, 0, 0, 20 * '\0'))
        fw.write(CRC_STRUCT.pack(0))

    return diroffsets, dirwritedataoffsets


def write_fake_file_offsets(fw, indexentries):
    beginning = fw.tell()
    for f in indexentries:
        fw.write(OFFSET_STRUCT.pack(0))
    return beginning


def write_dir_offsets(fw, offsets):
    # Skip the header
    fw.seek(HEADER_SIZE)
    for o in offsets:
        fw.write(OFFSET_STRUCT.pack(o))


def write_file_entry(fw, entry, offset):
    partialcrc = calculate_crc(OFFSET_STRUCT.pack(offset))
    partialcrc = write_calc_crc(fw, entry.filename + "\0", partialcrc)

    # Prepare flags
    flags = entry.flags & 0b1000000000000000
    flags += (entry.flags & 0b0011000000000000) * 2

    # calculate crc for stat data
    stat_crc = calculate_crc(STAT_DATA_CRC_STRUCT.pack(offset,
        entry.ctimesec, entry.ctimensec, entry.ino,
        entry.filesize, entry.dev, entry.uid, entry.gid))

    stat_data = FILE_DATA_STRUCT.pack(flags, entry.mode,
            entry.mtimesec, entry.mtimensec, stat_crc, entry.sha1)
    partialcrc = write_calc_crc(fw, stat_data, partialcrc)

    fw.write(CRC_STRUCT.pack(partialcrc))


def write_file_data(fw, indexentries):
    dirdata = dict()
    fileoffsets = list()
    for entry in sorted(indexentries, key=lambda k: k.pathname):
        offset = fw.tell()
        fileoffsets.append(offset)
        write_file_entry(fw, entry, offset)
        if entry.pathname not in dirdata:
            dirdata[entry.pathname] = DirEntry()

        dirdata[entry.pathname].nfiles += 1

    return fileoffsets, dirdata


def write_file_offsets(fw, foffsets, fileoffsetbeginning):
    fw.seek(fileoffsetbeginning)
    for f in foffsets:
        fw.write(OFFSET_STRUCT.pack(f))


def write_directory_data(fw, dirdata, dirwritedataoffsets,
        fileoffsetbeginning):
    foffset = fileoffsetbeginning
    for (pathname, entry) in sorted(dirdata.iteritems()):
        try:
            fw.seek(dirwritedataoffsets[pathname])
        except KeyError:
            continue

        if pathname == "":
            partialcrc = calculate_crc(pathname + "\0")
        else:
            partialcrc = calculate_crc(pathname + "/\0")


        partialcrc = write_calc_crc(fw, DIRECTORY_DATA_STRUCT.pack(entry.flags,
            foffset, entry.cr, entry.ncr, entry.nsubtrees, entry.nfiles,
            entry.nentries, entry.objname), partialcrc)

        foffset += entry.nfiles * 4

        fw.write(CRC_STRUCT.pack(partialcrc))


def write_conflicted_data(fw, conflictedentries, reucdata, dirdata):
    pass

def compile_cache_tree_data(dirdata, extensiondata):
    for (path, entry) in extensiondata.iteritems():
        dirdata[path.strip("/")].nentries = \
                int(entry.entry_count)

        dirdata[path.strip("/")].nsubtrees = \
                int(entry.subtrees)

        if entry.sha1 != "invalid":
            dirdata[path.strip("/")].objname = entry.sha1

    return dirdata


def read_index():
    r = Reader()
    header = read_header(r)

    (indexentries, conflictedentries, paths, files) = read_index_entries(r,
            header)

    treeextensiondata = dict()
    reucextensiondata = list()
    ext = r.read_without_updating_sha1(4)

    if ext == "TREE" or ext == "REUC":
        r.updateSha1(ext)
        if ext == "TREE":
            treeextensiondata = read_tree_extensiondata(r)
        else:
            reucextensiondata = read_reuc_extensiondata(r)
        ext = r.read_without_updating_sha1(4)

        if ext == "REUC":
            r.updateSha1(ext)
            reucextensiondata = read_reuc_extensiondata(r)

    sha1 = r.getSha1()

    if ext == "TREE" or ext == "REUC":
        sha1read = r.read_without_updating_sha1(20)
    else:
        sha1read = ext + r.read_without_updating_sha1(16)

    if sha1.hexdigest() != binascii.hexlify(sha1read):
        raise SHAError("SHA-1 code of the file doesn't match")

    return (header, indexentries, conflictedentries, paths, files,
            treeextensiondata, reucextensiondata)


def write_index_v5(header, indexentries, conflictedentries, paths, files,
        treeextensiondata, reucextensiondata):
    fw = open(".git/index-v5", "wb")

    write_header(fw, header, paths, files)
    write_fake_dir_offsets(fw, paths)
    (diroffsets, dirwritedataoffsets) = write_directories(fw,
            paths)

    fileoffsetbeginning = write_fake_file_offsets(fw, indexentries)
    fileoffsets, dirdata = write_file_data(fw, indexentries)

    # dirdata = write_conflicted_data(fw, conflictedentries,
    #         reucextensiondata, dirdata)

    write_dir_offsets(fw, diroffsets)
    write_file_offsets(fw, fileoffsets, fileoffsetbeginning)

    dirdata = compile_cache_tree_data(dirdata, treeextensiondata)
    write_directory_data(fw, dirdata, dirwritedataoffsets,
            fileoffsetbeginning)


def main(args):
    (header, indexentries, conflictedentries, paths, files, treeextensiondata,
            reucextensiondata) = read_index()

    for a in args:
        if a == "-h":
            print_header(header)
        if a == "-i":
            print_indexentries(indexentries)
        if a == "-c":
            print_extensiondata(treeextensiondata)
        if a == "-u":
            print_reucextensiondata(reucextensiondata)

    write_index_v5(header, indexentries, conflictedentries, paths, files,
            treeextensiondata, reucextensiondata)

if __name__ == "__main__":
    main(sys.argv[1:])
