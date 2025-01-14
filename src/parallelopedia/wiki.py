"""
A very simple wiki search HTTP server that demonstrates useful techniques
afforded by PyParallel: the ability to load large reference data structures
into memory, and then query them as part of incoming request processing in
parallel.
"""

import glob
import json
import logging
import mmap

# =============================================================================
# Imports
# =============================================================================
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from os.path import dirname
from typing import List, Tuple

import datrie
import mwcomposerfromhell as mwc
import mwparserfromhell as mwp
import numpy as np
from numpy import uint64

from parallelopedia.http.server import (
    HttpApp,
    RangedRequest,
    Request,
    date_time_string,
    make_routes,
    router,
)

from .util import join_path

# =============================================================================
# Configurables -- Change These!
# =============================================================================
# Change this to the directory containing the downloaded files.
# DATA_DIR = r'd:\data'
DATA_DIR = join_path(dirname(__file__), '../../data')

# =============================================================================
# Constants
# =============================================================================

# This file is huge when unzipped -- ~53GB.  Although, granted, it is the
# entire Wikipedia in a single file.  The bz2 version is much smaller, but
# still pretty huge.  Search the web for instructions on how to download
# from one of the wiki mirrors, then bunzip2 it, then place in the same
# data directory.
WIKI_XML_NAME = 'enwiki-20150205-pages-articles.xml'
WIKI_XML_PATH = join_path(DATA_DIR, WIKI_XML_NAME)

# The following two files can be downloaded from
# http://download.pyparallel.org.

# This is a trie keyed by every <title>xxx</title> in the wiki XML; the value
# is a 64-bit byte offset within the file where the title was found.
# Specifically, it is the offset where the '<' bit of the <title> was found.
TITLES_TRIE_PATH = join_path(DATA_DIR, 'titles.trie')
# And because this file is so huge and the data structure takes so long to
# load, we have another version that was created for titles starting with Z
# and z (I picked 'z' as I figured it would have the least-ish titles).  (This
# file was created via the save_titles_startingwith_z() method below.)
ZTITLES_TRIE_PATH = join_path(DATA_DIR, 'ztitles.trie')

# This is a sorted numpy array of uint64s representing the byte offset values
# in the trie.  When given the byte offset of a title derived from a trie
# lookup, we can find the byte offset of where the next title starts within
# the xml file.  That allows us to isolate the required byte range from the
# xml file where the particular title is defined.  Such a byte range can be
# satisfied with a ranged HTTP request.
TITLES_OFFSETS_NPY_PATH = join_path(DATA_DIR, 'titles_offsets.npy')

PARTITIONS = 127

# =============================================================================
# Aliases
# =============================================================================
uint64_7 = uint64(7)
uint64_11 = uint64(11)


# =============================================================================
# Trie Helpers
# =============================================================================
def get_wiki_tries_in_dir(directory: str) -> List[str]:
    return sorted(glob.glob(f'{directory}/wiki-*.trie'))


def get_wiki_tries(directory: str) -> dict:
    paths = get_wiki_tries_in_dir(directory)
    basename = os.path.basename
    result = {}
    for path in paths:
        base = basename(path).replace('wiki-', '').replace('.trie', '')
        parts = base.split('_')
        assert len(parts) == 2, f'Invalid filename: {base}'
        (ordinal, length) = parts
        char = chr(int(ordinal))
        result[char] = path
    return result


def load_trie(path: str) -> datrie.Trie:
    msg_prefix = f'[{threading.get_native_id()}]'
    start = time.perf_counter()
    logging.debug(f'{msg_prefix} Loading {path}...')
    trie = datrie.Trie.load(path)
    end = time.perf_counter()
    elapsed = end - start
    logging.debug(f'{msg_prefix} Loaded {path} in {elapsed:.4f} seconds.')
    return trie


def load_wiki_tries_parallel(
    directory: str, max_threads: int = 0
) -> List[datrie.Trie]:

    if max_threads < 1:
        max_threads = os.cpu_count()

    paths_by_first_char = get_wiki_tries(directory)
    tries = [None] * PARTITIONS
    num_tries = len(paths_by_first_char)
    print(
        f'Loading {num_tries} tries in parallel with '
        f'{max_threads} threads...'
    )
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        futures = {
            executor.submit(load_trie, path): (char, path)
            for (char, path) in paths_by_first_char.items()
        }
        for future in as_completed(futures):
            (char, path) = futures[future]
            try:
                trie = future.result()
                assert trie is not None, f'Failed to load {path}'
                assert ord(char) not in tries, f'Duplicate trie for {char}'
                tries[ord(char)] = trie
            except Exception as e:
                print(f'Error loading {path}: {e}')
    end = time.perf_counter()
    elapsed = end - start
    print(f'Loaded {num_tries} tries in {elapsed:.4f} seconds.')
    return tries


# =============================================================================
# Globals
# =============================================================================
WIKI_XML_FILE = open(WIKI_XML_PATH, 'rb')
WIKI_XML_STAT = os.fstat(WIKI_XML_FILE.fileno())
WIKI_XML_SIZE = WIKI_XML_STAT.st_size
WIKI_XML_LAST_MODIFIED = date_time_string(WIKI_XML_STAT.st_mtime)
WIKI_XML_MMAP = mmap.mmap(
    WIKI_XML_FILE.fileno(),
    length=0,
    flags=mmap.MAP_SHARED,
    prot=mmap.PROT_READ,
    offset=0,
)
try:
    WIKI_XML_MMAP.madvise(mmap.MADV_RANDOM)
except AttributeError:
    # Ignore if madvise is not available.
    pass

TITLE_OFFSETS = np.load(TITLES_OFFSETS_NPY_PATH)

# Now load all of the tries in parallel.
TITLE_TRIES = load_wiki_tries_parallel(DATA_DIR)


# =============================================================================
# Misc Helpers
# =============================================================================
def json_serialization(request: Request = None, obj: dict = None) -> Request:
    """
    Helper method for converting a dict `obj` into a JSON response for the
    incoming `request`.
    """
    if not request:
        request = Request(transport=None, data=None)
    if not obj:
        obj = {}
    response = request.response
    response.code = 200
    response.message = 'OK'
    response.content_type = 'application/json; charset=UTF-8'
    response.body = json.dumps(obj)

    return request


def text_serialization(request=None, text=None):
    if not request:
        request = Request(transport=None, data=None)
    if not text:
        text = 'Hello, World!'
    response = request.response
    response.code = 200
    response.message = 'OK'
    response.content_type = 'text/plain; charset=UTF-8'
    response.body = text

    return request


# =============================================================================
# Offset Helpers
# =============================================================================
def get_page_offsets_for_key(search_string: str) -> List[Tuple[str, int, int]]:
    """
    Given a search string, return a list of tuples of the form
    (title, start_offset, end_offset) where start_offset and end_offset
    represent the byte offsets within the wiki XML file where the content
    for this title starts and ends, respectively.
    """
    if len(search_string) < 1:
        return None
    results = []
    titles = TITLE_TRIES[ord(search_string[0])]
    if not titles:
        return results
    items = titles.items(search_string)
    if not items:
        return results
    offsets = TITLE_OFFSETS
    for key, value in items:
        v = value[0]
        o = uint64(v if v > 0 else v * -1)
        ix = offsets.searchsorted(o, side='right')
        results.append((key, int(o - uint64_7), int(offsets[ix] - uint64_11)))
    return results


# =============================================================================
# Web Helpers
# =============================================================================
def exact_title(title):
    if len(title) < 1:
        return json.dumps([])

    titles = TITLE_TRIES[ord(title[0])]
    if title in titles:
        return json.dumps(
            [
                [
                    title,
                ]
                + [t for t in titles[title]]
            ]
        )
    else:
        return json.dumps([])


# =============================================================================
# Classes
# =============================================================================
class WikiApp(HttpApp):
    routes = make_routes()
    route = router(routes)

    use_mmap = True

    @route
    def wiki(self, request, name, **kwds):
        # Do an exact lookup if we find a match.
        if len(name) < 1:
            return self.error(request, 400, "Name too short (< 1 char)")

        titles = TITLE_TRIES[ord(name[0])]
        if not titles or name not in titles:
            return self.error(request, 404)

        o = titles[name][0]
        o = uint64(o if o > 0 else o * -1)
        offsets = TITLE_OFFSETS
        ix = offsets.searchsorted(o, side='right')
        start = int(o - uint64_7)
        end = int(offsets[ix] - uint64_11)
        range_request = '%d-%d' % (start, end)
        request.range = RangedRequest(range_request)
        request.response.content_type = 'text/xml; charset=utf-8'
        return self.ranged_sendfile_mmap(
            request,
            WIKI_XML_MMAP,
            WIKI_XML_SIZE,
            WIKI_XML_LAST_MODIFIED,
        )

    @route
    def offsets(self, request, name, limit=None):
        if not name:
            return self.error(request, 400, "Missing name")

        if len(name) < 3:
            return self.error(request, 400, "Name too short (< 3 chars)")

        return self.send_response(
            json_serialization(request, get_page_offsets_for_key(name))
        )

    @route
    def xml(self, request, *args, **kwds):
        if not request.range:
            return self.error(request, 400, "Ranged-request required.")
        else:
            request.response.content_type = 'text/xml; charset=utf-8'
            return self.ranged_sendfile_mmap(
                request,
                WIKI_XML_MMAP,
                WIKI_XML_SIZE,
                WIKI_XML_LAST_MODIFIED,
            )

    @route
    def html(self, request, *args, **kwds):
        rr = request.range
        if not rr:
            return self.error(request, 400, "Ranged-request required.")

        if not rr.set_file_size_safe(WIKI_XML_SIZE, self):
            return

        response = request.response
        response.code = 200
        response.message = 'OK'
        response.content_type = 'text/html; charset=UTF-8'

        file_content = WIKI_XML_MMAP[rr.first_byte:rr.last_byte + 1]

        code = mwp.parse(file_content)
        html = mwc.compose(code)
        response.content_length = len(html)
        response.body = html

        return self.send_response(request)

    @route
    def hello(self, request, *args, **kwds):
        j = {'args': args, 'kwds': kwds}
        return json_serialization(request, j)

    @route
    def title(self, request, name, *args, **kwds):
        if len(name) < 1:
            return self.error(request, 400, "Name too short (< 1 char)")

        titles = TITLE_TRIES[ord(name[0])]
        if not titles or name not in titles:
            return self.error(request, 404)

        items = titles.items(name)
        return self.send_response(json_serialization(request, items))

    @route
    def json(self, request, *args, **kwds):
        return self.send_response(
            json_serialization(request, {'message': 'Hello, World!'})
        )

    @route
    def plaintext(self, request, *args, **kwds):
        return self.send_response(
            text_serialization(request, text='Hello, World!')
        )


# vim:set ts=8 sw=4 sts=4 tw=78 et:                                          #
