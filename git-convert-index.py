#!/usr/bin/env python

# Outputs: Calculated and read sha1 checksum in hex format
# Usage: python git-convert-index.py
# Read the index format with git-read-index-v5.py
# read-index-v5 outputs the same format as git ls-files

import hashlib
import binascii
import struct
import os.path
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


HEADER_SIZE = 24

# File formats
HEADER_FORMAT = """\
Signature: %(signature)s
Version: %(version)s
Number of entries: %(nrofentries)s"""

ENTRIES_FORMAT = """\
ctime: %(ctimesec)s:%(ctimensec)s
mtime: %(mtimesec)s:%(mtimensec)s
dev: %(dev)s\tino: %(ino)s
uid: %(uid)s\tgid: %(gid)s
size: %(filesize)s\tflags: """

EXTENSION_FORMAT = """\
%(sha1)s %(path)s (%(entry_count)s entries, %(subtrees)s subtrees)"""

REUCEXTENSION_FORMAT = """\
Path: %(path)s
Entrymode 1: %(entry_mode0)s Entrymode 2: %(entry_mode1)s Entrymode 3:\
        %(entry_mode2)s"""

EXTENSION_FORMAT_WITHOUT_SHA = """\
invalid %(path)s (%(entry_count)s entries, %(subtrees)s subtrees)"""

HEADER_STRUCT = struct.Struct("!4sII")
HEADER_V5_STRUCT = struct.Struct("!4sIIII")

STAT_DATA_STRUCT = struct.Struct("!IIIIIIIIII 20sh")
STAT_DATA_STRUCT_EXTENDED_FLAGS = struct.Struct("!IIIIIIIIII 20shh")

CRC_STRUCT = struct.Struct("!i")

DIRECTORY_DATA_STRUCT = struct.Struct("!HIIIIII 20s")

STAT_DATA_CRC_STRUCT = struct.Struct("!IIIIIIII")

FILE_DATA_STRUCT = struct.Struct("!HHIIi 20s")


def write_calc_crc(fw, data, partialcrc=0):
    fw.write(data)
    crc = binascii.crc32(data, partialcrc)
    return crc


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
    return dict(signature=signature, version=version, nrofentries=nrofentries)


def read_entry(r, header):
    if header["version"] == 3:
        entry = STAT_DATA_STRUCT_EXTENDED_FLAGS.unpack(
                r.read(STAT_DATA_STRUCT_EXTENDED_FLAGS.size))
    else:
        entry = STAT_DATA_STRUCT.unpack(
                r.read(STAT_DATA_STRUCT.size))

    (name, readbytes) = read_name(r, '\0')

    pathname = os.path.dirname(name)
    filename = os.path.basename(name)

    entry = entry + (pathname, filename)           # Filename

    if (header["version"] == 3):
        dictentry = dict(zip(('ctimesec', 'ctimensec', 'mtimesec',
            'mtimensec', 'dev', 'ino', 'mode', 'uid', 'gid', 'filesize',
            'sha1', 'flags', 'xtflags', 'pathname', 'filename'), entry))
    else:
        dictentry = dict(zip(('ctimesec', 'ctimensec', 'mtimesec',
            'mtimensec', 'dev', 'ino', 'mode', 'uid', 'gid', 'filesize',
            'sha1', 'flags', 'pathname', 'filename'), entry))

    if header["version"] == 2:
        j = 8 - (readbytes + 5) % 8
    else:
        j = 8 - (readbytes + 1) % 8

    # Just throw the padding away.
    r.read(j - 1)

    return dictentry


def read_index_entries(r):
    indexentries = []
    conflictedentries = defaultdict(list)
    paths = set()
    files = list()
    filedirs = defaultdict(list)
    # Read index entries
    for i in xrange(header["nrofentries"]):
        entry = read_entry(r, header)

        paths.add(entry["pathname"])
        files.append(entry["filename"])
        filedirs[entry["pathname"]].append(entry["filename"])

        stage = (entry['flags'] & 0b0011000000000000) / 0b001000000000000

        if stage == 0:      # Not conflicted
            indexentries.append(entry)
        else:                   # Conflicted
            if stage == 1:
                # Write the stage 1 entry to the main index, to avoid
                # rewriting the whole index once the conflict is resolved
                indexentries.append(entry)
            conflictedentries[entry["pathname"]].append(entry)

    return indexentries, conflictedentries, paths, files, filedirs


def read_extensiondata(r):
    extensionsize = r.read(4)

    read = 0
    subtreenr = [0]
    subtree = [""]
    listsize = 0
    extensiondata = dict()
    while read < int(struct.unpack("!I", extensionsize)[0]):
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

        if sha1 == "invalid":
            extensiondata[fpath] = dict(path=fpath,
                entry_count=entry_count, subtrees=subtrees)
        else:
            extensiondata[fpath] = dict(path=fpath,
                entry_count=entry_count, subtrees=subtrees, sha1=sha1)

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

    return dict(path=path, entry_mode0=entry_mode[0], entry_mode1=entry_mode[1],
            entry_mode2=entry_mode[2], obj_names0=obj_names[0],
            obj_names1=obj_names[1], obj_names2=obj_names[2]), read


def read_reuc_extensiondata(r):
    extensionsize = r.read(4)

    read = 0
    extensiondata = defaultdict(list)
    while read < int(struct.unpack("!I", extensionsize)[0]):
        (entry, readbytes) = read_reuc_extension_entry(r)
        read += readbytes
        extensiondata["/".join(entry["path"].split("/"))[:-1]].append(entry)

    return extensiondata


def print_header(header):
    print HEADER_FORMAT % header


def print_indexentries(indexentries):
    for entry in indexentries:
        if entry["pathname"] != "":
            print entry["pathname"] + "/" + entry["filename"]
        else:
            print entry["filename"]
        print ENTRIES_FORMAT % entry + "%x" % entry["flags"]


def print_extensiondata(extensiondata):
    for entry in sorted(extensiondata.itervalues()):
        try:
            print EXTENSION_FORMAT % entry
        except KeyError:
            print EXTENSION_FORMAT_WITHOUT_SHA % entry


def print_reucextensiondata(extensiondata):
    for e in extensiondata:
        print REUCEXTENSION_FORMAT % e
        print ("Objectnames 1: " + binascii.hexlify(e["obj_names0"]) +
                " Objectnames 2: " + binascii.hexlify(e["obj_names1"]) +
                " Objectnames 3: " + binascii.hexlify(e["obj_names2"]))


def writev5_1header(fw, header, paths, files):
    crc = write_calc_crc(fw, HEADER_V5_STRUCT.pack(header["signature"], 5,
        len(paths), len(files), 0))
    fw.write(CRC_STRUCT.pack(crc))


def writev5_1fakediroffsets(fw, paths):
    for p in paths:
        fw.write(struct.pack("!I", 0))


def writev5_1directories(fw, paths):
    diroffsets = list()
    dirwritedataoffsets = dict()
    dirdata = defaultdict(dict)
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

    return diroffsets, dirwritedataoffsets, dirdata


def writev5_1fakefileoffsets(fw, indexentries):
    beginning = fw.tell()
    for f in indexentries:
        fw.write(struct.pack("!I", 0))
    return beginning


def writev5_1diroffsets(fw, offsets):
    # Skip the header
    fw.seek(HEADER_SIZE)
    for o in offsets:
        fw.write(struct.pack("!I", o))


def writev5_1filedata(fw, indexentries, dirdata):
    fileoffsets = list()
    for entry in sorted(indexentries, key=lambda k: k['pathname']):
        offset = fw.tell()
        fileoffsets.append(offset)
        partialcrc = binascii.crc32(struct.pack("!I", fw.tell()))
        partialcrc = write_calc_crc(fw, entry["filename"] + "\0", partialcrc)

        # Prepare flags
        flags = entry["flags"] & 0b1000000000000000
        flags += (entry["flags"] & 0b0011000000000000) * 2

        # calculate crc for stat data
        stat_crc = binascii.crc32(STAT_DATA_CRC_STRUCT.pack(offset,
            entry["ctimesec"], entry["ctimensec"], entry["ino"],
            entry["filesize"], entry["dev"], entry["uid"], entry["gid"]))

        stat_data = FILE_DATA_STRUCT.pack(flags, entry["mode"],
                entry["mtimesec"], entry["mtimensec"], stat_crc, entry["sha1"])
        partialcrc = write_calc_crc(fw, stat_data, partialcrc)

        fw.write(CRC_STRUCT.pack(partialcrc))
        try:
            dirdata[entry["pathname"]]["nfiles"] += 1
        except KeyError:
            dirdata[entry["pathname"]]["nfiles"] = 1

    return fileoffsets, dirdata


def writev5_1fileoffsets(fw, foffsets, fileoffsetbeginning):
    fw.seek(fileoffsetbeginning)
    for f in foffsets:
        fw.write(struct.pack("!I", f))


def writev5_1directorydata(fw, dirdata, dirwritedataoffsets, 
        fileoffsetbeginning):
    foffset = fileoffsetbeginning
    for d in sorted(dirdata.iteritems()):
        try:
            fw.seek(dirwritedataoffsets[d[0]])
        except KeyError:
            continue

        if d[0] == "":
            partialcrc = binascii.crc32(d[0] + "\0")
        else:
            partialcrc = binascii.crc32(d[0] + "/\0")

        try:
            flags = d[1]["flags"]
        except KeyError:
            flags = 0

        try:
            cr = d[1]["cr"]
        except KeyError:
            cr = 0

        try:
            ncr = d[1]["ncr"]
        except KeyError:
            ncr = 0

        try:
            nsubtrees = d[1]["nsubtrees"]
        except KeyError:
            nsubtrees = 0

        try:
            nfiles = d[1]["nfiles"]
        except KeyError:
            nfiles = 0

        try:
            nentries = d[1]["nentries"]
        except KeyError:
            nentries = 0

        try:
            objname = binascii.unhexlify(d[1]["objname"])
        except KeyError:
            objname = 20 * '\0'

        if nfiles == -1:
            nfiles = 0


        partialcrc = write_calc_crc(fw, DIRECTORY_DATA_STRUCT.pack(flags,
            foffset, cr, ncr, nsubtrees, nfiles, nentries, objname), partialcrc)

        foffset += nfiles * 4

        fw.write(struct.pack("!i", partialcrc))


def writev5_1conflicteddata(fw, conflictedentries, reucdata, dirdata):
    for d in sorted(conflictedentries):
        for f in d:
            if d["pathname"] == "":
                filename = d["filename"]
            else:
                filename = d["pathname"] + "/" + d["filename"]

            dirdata[filename]["cr"] = fw.tell()
            try:
                dirdata[filename]["ncr"] += 1
            except KeyError:
                dirdata[filename]["ncr"] = 1

            partialcrc = write_calc_crc(fw, d["pathname"] + d["filename"] + "\0")
            stages = set()
            partialcrc = write_calc_crc(fw, struct.pack("!b", 0), partialcrc)
            for i in xrange(0, 2):
                partialcrc = write_calc_crc(fw, struct.pack("!i", d["mode"]),
                        partialcrc)
                if d["mode"] != 0:
                    stages.add(i)

            for i in sorted(stages):
                partialcrc = write_calc_crc(fw, binascii.unhexlify(d["sha1"]),
                        partialcrc)

            fw.write(struct.pack("!i", partialcrc))

    return dirdata


def compilev5_1cachetreedata(dirdata, extensiondata):
    for entry in extensiondata.iteritems():
        dirdata[entry[1]["path"].strip("/")]["nentries"] = \
                int(entry[1]["entry_count"])
        try:
            dirdata[entry[1]["path"].strip("/")]["objname"] = entry[1]["sha1"]
        except:
            continue  # Cache tree invalid

        try:
            dirdata[entry[1]["path"].strip("/")]["nsubtrees"] = \
                    entry[1]["subtreenr"]
        except KeyError:
            pass

    return dirdata


r = Reader()
header = read_header(r)

(indexentries, conflictedentries, paths, files,
        filedirs) = read_index_entries(r)

ext = r.read_without_updating_sha1(4)
extensiondata = []

ext2 = ""
if ext == "TREE":
    r.updateSha1(ext)
    treeextensiondata = read_extensiondata(r)
    ext2 = r.read_without_updating_sha1(4)
else:
    treeextensiondata = dict()

if ext == "REUC" or ext2 == "REUC":
    if ext == "REUC":
        r.updateSha1(ext)
    else:
        r.updateSha1(ext2)
    reucextensiondata = read_reuc_extensiondata(r)
else:
    reucextensiondata = list()

print_header(header)
print_indexentries(indexentries)
print_extensiondata(treeextensiondata)
print_reucextensiondata(reucextensiondata)

sha1 = r.getSha1()

if ext != "TREE" and ext != "REUC" and ext2 != "REUC":
    sha1read = ext + r.read_without_updating_sha1(16)
elif ext2 != "REUC" and ext == "TREE":
    sha1read = ext2 + r.read_without_updating_sha1(16)
else:
    sha1read = r.read_without_updating_sha1(20)

print "SHA1 over the whole file: " + str(binascii.hexlify(sha1read))

print "SHA1 over filedata: " + str(sha1.hexdigest())

if sha1.hexdigest() == binascii.hexlify(sha1read):
    fw = open(".git/index-v5", "wb")

    writev5_1header(fw, header, paths, files)
    writev5_1fakediroffsets(fw, paths)
    (diroffsets, dirwritedataoffsets, dirdata) = writev5_1directories(fw,
            paths)
    fileoffsetbeginning = writev5_1fakefileoffsets(fw, indexentries)
    fileoffsets, dirdata = writev5_1filedata(fw, indexentries, dirdata)
    dirdata = writev5_1conflicteddata(fw, conflictedentries, reucextensiondata,
            dirdata)
    writev5_1diroffsets(fw, diroffsets)
    writev5_1fileoffsets(fw, fileoffsets, fileoffsetbeginning)
    dirdata = compilev5_1cachetreedata(dirdata, treeextensiondata)
    writev5_1directorydata(fw, dirdata, dirwritedataoffsets,
            fileoffsetbeginning)
else:
    print "File is corrupted"
