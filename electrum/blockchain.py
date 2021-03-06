# Electrum - lightweight Bitcoin client
# Copyright (C) 2012 thomasv@ecdsa.org
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import os
import threading
from typing import Optional, Dict, Mapping, Sequence

from . import util
from .bitcoin import hash_encode, int_to_hex, rev_hex
from .crypto import sha256d
from . import constants
from .util import bfh, bh2u
from .simple_config import SimpleConfig
from .logging import get_logger, Logger
from .bitcoin import get_header_size, is_post_equihash_fork, HDR_LEN, HDR_LEN_FORK

_logger = get_logger(__name__)

CHUNK_LEN = 200
HEADER_SIZE = 1487  # bytes

MAX_TARGET = 0x0007FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
MIN_TARGET = 0x0007ffff00000000000000000000000000000000000000000000000000000000
POW_AVERAGING_WINDOW = 17
POW_MEDIAN_BLOCK_SPAN = 11
POW_MAX_ADJUST_DOWN = 32
POW_MAX_ADJUST_UP = 16
POW_DAMPING_FACTOR = 4
POW_TARGET_SPACING = 60
EH_EPOCH_1_END = 266000
LWMA_FORK_BLOCK = 765000
ZAWY_LWMA3_AVERAGING_WINDOW = 60

USE_COMPRESSSION = False
COMPRESSION_LEVEL = 1

TARGET_CALC_BLOCKS = POW_AVERAGING_WINDOW + POW_MEDIAN_BLOCK_SPAN

AVERAGING_WINDOW_TIMESPAN = POW_AVERAGING_WINDOW * POW_TARGET_SPACING

MIN_ACTUAL_TIMESPAN = AVERAGING_WINDOW_TIMESPAN * \
    (100 - POW_MAX_ADJUST_UP) // 100

MAX_ACTUAL_TIMESPAN = AVERAGING_WINDOW_TIMESPAN * \
    (100 + POW_MAX_ADJUST_DOWN) // 100


class MissingHeader(Exception):
    pass

class InvalidHeader(Exception):
    pass

def serialize_header(res):
    s = int_to_hex(res.get('version'), 4) \
        + rev_hex(res.get('prev_block_hash')) \
        + rev_hex(res.get('merkle_root')) \
        + rev_hex(res.get('reserved_hash')) \
        + int_to_hex(int(res.get('timestamp')), 4) \
        + int_to_hex(int(res.get('bits')), 4) \
        + rev_hex(res.get('nonce')) \
        + rev_hex(res.get('sol_size')) \
        + rev_hex(res.get('solution'))
    return s

def deserialize_header(s, height):
    if not s:
        raise Exception('Invalid header: {}'.format(s))
    
    if len(s) != get_header_size(height):
        raise Exception('Invalid header length: {}'.format(len(s)))
    hex_to_int = lambda s: int('0x' + bh2u(s[::-1]), 16)
    h = {}
    h['version'] = hex_to_int(s[0:4])
    h['prev_block_hash'] = hash_encode(s[4:36])
    h['merkle_root'] = hash_encode(s[36:68])
    h['reserved_hash'] = hash_encode(s[68:100])
    h['timestamp'] = hex_to_int(s[100:104])
    h['bits'] = hex_to_int(s[104:108])
    h['nonce'] = hash_encode(s[108:140])
    h['sol_size'] = hash_encode(s[140:143])
    h['solution'] = hash_encode(s[143:])
    h['block_height'] = height
    return h

def hash_header(header: dict) -> str:
    if header is None:
        return '0' * 64
    if header.get('prev_block_hash') is None:
        header['prev_block_hash'] = '00'*32
    return hash_encode(sha256d(bfh(serialize_header(header))))


def hash_raw_header(header: str) -> str:
    return hash_encode(sha256d(bfh(header)))


# key: blockhash hex at forkpoint
# the chain at some key is the best chain that includes the given hash
blockchains = {}  # type: Dict[str, Blockchain]
blockchains_lock = threading.RLock()


def read_blockchains(config: 'SimpleConfig'):
    best_chain = Blockchain(config=config,
                            forkpoint=0,
                            parent=None,
                            forkpoint_hash=constants.net.GENESIS,
                            prev_hash=None)
    blockchains[constants.net.GENESIS] = best_chain
    # consistency checks
    if best_chain.height() > constants.net.max_checkpoint():
        header_after_cp = best_chain.read_header(constants.net.max_checkpoint()+1)
        if not header_after_cp or not best_chain.can_connect(header_after_cp, check_height=False):
            _logger.info("[blockchain] deleting best chain. cannot connect header after last cp to last cp.")
            os.unlink(best_chain.path())
            best_chain.update_size(best_chain.size())
    # forks
    fdir = os.path.join(util.get_headers_dir(config), 'forks')
    util.make_dir(fdir)
    # files are named as: fork2_{forkpoint}_{prev_hash}_{first_hash}
    l = filter(lambda x: x.startswith('fork2_') and '.' not in x, os.listdir(fdir))
    l = sorted(l, key=lambda x: int(x.split('_')[1]))  # sort by forkpoint

    def delete_chain(filename, reason):
        _logger.info(f"[blockchain] deleting chain {filename}: {reason}")
        os.unlink(os.path.join(fdir, filename))

    def instantiate_chain(filename):
        __, forkpoint, prev_hash, first_hash = filename.split('_')
        forkpoint = int(forkpoint)
        prev_hash = (64-len(prev_hash)) * "0" + prev_hash  # left-pad with zeroes
        first_hash = (64-len(first_hash)) * "0" + first_hash
        # forks below the max checkpoint are not allowed
        if forkpoint <= constants.net.max_checkpoint():
            delete_chain(filename, "deleting fork below max checkpoint")
            return
        # find parent (sorting by forkpoint guarantees it's already instantiated)
        for parent in blockchains.values():
            if parent.check_hash(forkpoint - 1, prev_hash):
                break
        else:
            delete_chain(filename, "cannot find parent for chain")
            return
        b = Blockchain(config=config,
                       forkpoint=forkpoint,
                       parent=parent,
                       forkpoint_hash=first_hash,
                       prev_hash=prev_hash)
        # consistency checks
        h = b.read_header(b.forkpoint)
        if first_hash != hash_header(h):
            delete_chain(filename, "incorrect first hash for chain")
            return
        if not b.parent.can_connect(h, check_height=False):
            delete_chain(filename, "cannot connect chain to parent")
            return
        chain_id = b.get_id()
        assert first_hash == chain_id, (first_hash, chain_id)
        blockchains[chain_id] = b

    for filename in l:
        instantiate_chain(filename)


def get_best_chain() -> 'Blockchain':
    return blockchains[constants.net.GENESIS]

# block hash -> chain work; up to and including that block
_CHAINWORK_CACHE = {
    "0000000000000000000000000000000000000000000000000000000000000000": 0,  # virtual block at height -1
}  # type: Dict[str, int]


class Blockchain(Logger):
    """
    Manages blockchain headers and their verification
    """

    def __init__(self, config: SimpleConfig, forkpoint: int, parent: Optional['Blockchain'],
                 forkpoint_hash: str, prev_hash: Optional[str]):
        assert isinstance(forkpoint_hash, str) and len(forkpoint_hash) == 64, forkpoint_hash
        assert (prev_hash is None) or (isinstance(prev_hash, str) and len(prev_hash) == 64), prev_hash
        # assert (parent is None) == (forkpoint == 0)
        if 0 < forkpoint <= constants.net.max_checkpoint():
            raise Exception(f"cannot fork below max checkpoint. forkpoint: {forkpoint}")
        Logger.__init__(self)
        self.config = config
        self.forkpoint = forkpoint  # height of first header
        self.parent = parent
        self._forkpoint_hash = forkpoint_hash  # blockhash at forkpoint. "first hash"
        self._prev_hash = prev_hash  # blockhash immediately before forkpoint
        self.lock = threading.RLock()
        self.update_size(0)

    def with_lock(func):
        def func_wrapper(self, *args, **kwargs):
            with self.lock:
                return func(self, *args, **kwargs)
        return func_wrapper

    @property
    def checkpoints(self):
        return constants.net.CHECKPOINTS

    def get_max_child(self) -> Optional[int]:
        children = self.get_direct_children()
        return max([x.forkpoint for x in children]) if children else None

    def get_max_forkpoint(self) -> int:
        """Returns the max height where there is a fork
        related to this chain.
        """
        mc = self.get_max_child()
        return mc if mc is not None else self.forkpoint

    def get_direct_children(self) -> Sequence['Blockchain']:
        with blockchains_lock:
            return list(filter(lambda y: y.parent==self, blockchains.values()))

    def get_parent_heights(self) -> Mapping['Blockchain', int]:
        """Returns map: (parent chain -> height of last common block)"""
        with blockchains_lock:
            result = {self: self.height()}
            chain = self
            while True:
                parent = chain.parent
                if parent is None: break
                result[parent] = chain.forkpoint - 1
                chain = parent
            return result

    def get_height_of_last_common_block_with_chain(self, other_chain: 'Blockchain') -> int:
        last_common_block_height = 0
        our_parents = self.get_parent_heights()
        their_parents = other_chain.get_parent_heights()
        for chain in our_parents:
            if chain in their_parents:
                h = min(our_parents[chain], their_parents[chain])
                last_common_block_height = max(last_common_block_height, h)
        return last_common_block_height

    @with_lock
    def get_branch_size(self) -> int:
        return self.height() - self.get_max_forkpoint() + 1

    def get_name(self) -> str:
        return self.get_hash(self.get_max_forkpoint()).lstrip('0')[0:10]

    def check_header(self, header: dict) -> bool:
        header_hash = hash_header(header)
        height = header.get('block_height')
        return self.check_hash(height, header_hash)

    def check_hash(self, height: int, header_hash: str) -> bool:
        """Returns whether the hash of the block at given height
        is the given hash.
        """
        assert isinstance(header_hash, str) and len(header_hash) == 64, header_hash  # hex
        try:
            return header_hash == self.get_hash(height)
        except Exception:
            return False
    
    def fork(parent, header: dict) -> 'Blockchain':
        if not parent.can_connect(header, check_height=False):
            raise Exception("forking header does not connect to parent chain")
        forkpoint = header.get('block_height')
        self = Blockchain(config=parent.config,
                          forkpoint=forkpoint,
                          parent=parent,
                          forkpoint_hash=hash_header(header),
                          prev_hash=parent.get_hash(forkpoint-1))
        open(self.path(), 'w+').close()
        self.save_header(header)
        # put into global dict. note that in some cases
        # save_header might have already put it there but that's OK
        chain_id = self.get_id()
        with blockchains_lock:
            blockchains[chain_id] = self
        return self

    @with_lock
    def height(self) -> int:
        return self.forkpoint + self.size() - 1

    @with_lock
    def size(self) -> int:
        return self._size

    @with_lock
    def update_size(self, height) -> None:
        p = self.path()
        if os.path.exists(p):
            with open(p, 'rb') as f:
                size = f.seek(0, 2)

            self._size = self.calculate_size(height, size)
        else:
            self._size = 0

    @with_lock
    def calculate_size(self, checkpoint, size_in_bytes):
        # Post-fork
        pob = 0
        if not is_post_equihash_fork(checkpoint):
            pob = (size_in_bytes // get_header_size(0))
            if is_post_equihash_fork(pob):
                pob = constants.net.EQUIHASH_FORK_HEIGHT
                checkpoint = constants.net.EQUIHASH_FORK_HEIGHT
                size_in_bytes -= (pob * get_header_size(0))
        else:
            pob = constants.net.EQUIHASH_FORK_HEIGHT
            size_in_bytes -= (pob * get_header_size(0))
        # Equihash-Fork
        peb = 0
        
        if is_post_equihash_fork(checkpoint):
            peb = size_in_bytes // get_header_size(constants.net.EQUIHASH_FORK_HEIGHT)
        return pob + peb

    @classmethod
    def verify_header(self, header: dict, prev_hash: str, target: int):
        _hash = hash_header(header)
        if prev_hash != header.get('prev_block_hash'):
            raise Exception("prev hash mismatch: %s vs %s" % (prev_hash, header.get('prev_block_hash')))
        if constants.net.TESTNET:
            return
        bits = self.target_to_bits(target)
        if bits != header.get('bits'):
            raise Exception("bits mismatch: %s vs %s" % (bits, header.get('bits')))
        if int('0x' + _hash, 16) > target:
            raise Exception("insufficient proof of work: %s vs target %s" % (int('0x' + _hash, 16), target))

    def verify_chunk(self, index: int, data: bytes) -> None:
        size = len(data)
        height = index * 200

        prev_hash = self.get_hash(height - 1)
        
        chunk_headers =  {'empty': True}
        offset = 0
        target = 0
        i = 0
        while offset < size:
            header_size = get_header_size(height)
            raw_header = data[offset:(offset + header_size)]
            header = deserialize_header(raw_header, height)
            #self.logger.info(f'header {header}')
            target = self.get_target(height, chunk_headers)
            self.verify_header(header, prev_hash, target)

            chunk_headers[height] = header
            if i == 0:
                chunk_headers['min_height'] = height
                chunk_headers['empty'] = False
            chunk_headers['max_height'] = height
            prev_hash = hash_header(header)
            offset += header_size
            height += 1
            i+=1

    @with_lock
    def path(self):
        d = util.get_headers_dir(self.config)
        if self.parent is None:
            filename = 'blockchain_headers'
        else:
            assert self.forkpoint > 0, self.forkpoint
            prev_hash = self._prev_hash.lstrip('0')
            first_hash = self._forkpoint_hash.lstrip('0')
            basename = f'fork2_{self.forkpoint}_{prev_hash}_{first_hash}'
            filename = os.path.join('forks', basename)
        return os.path.join(d, filename)

    @with_lock
    def save_chunk(self, index: int, chunk: bytes):
        assert index >= 0, index
        chunk_within_checkpoint_region = index < len(self.checkpoints)
        # chunks in checkpoint region are the responsibility of the 'main chain'
        if chunk_within_checkpoint_region and self.parent is not None:
            main_chain = get_best_chain()
            main_chain.save_chunk(index, chunk)
            return

        delta_height = (index * 200 - self.forkpoint)
        delta_bytes = self.get_offset(self.forkpoint, delta_height)
        # if this chunk contains our forkpoint, only save the part after forkpoint
        # (the part before is the responsibility of the parent)
        if delta_bytes < 0:
            chunk = chunk[-delta_bytes:]
            delta_bytes = 0
        truncate = not chunk_within_checkpoint_region
        self.write(chunk, delta_bytes, truncate)
        self.swap_with_parent()

    def swap_with_parent(self) -> None:
        with self.lock, blockchains_lock:
            # do the swap; possibly multiple ones
            cnt = 0
            while True:
                old_parent = self.parent
                if not self._swap_with_parent():
                    break
                # make sure we are making progress
                cnt += 1
                if cnt > len(blockchains):
                    raise Exception(f'swapping fork with parent too many times: {cnt}')
                # we might have become the parent of some of our former siblings
                for old_sibling in old_parent.get_direct_children():
                    if self.check_hash(old_sibling.forkpoint - 1, old_sibling._prev_hash):
                        old_sibling.parent = self

    def _swap_with_parent(self) -> bool:
        """Check if this chain became stronger than its parent, and swap
        the underlying files if so. The Blockchain instances will keep
        'containing' the same headers, but their ids change and so
        they will be stored in different files."""
        if self.parent is None:
            return False

        self.logger.info(f"swapping {self.forkpoint} {self.parent.forkpoint}")
        parent_branch_size = self.parent.height() - self.forkpoint + 1
        forkpoint = self.forkpoint  # type: Optional[int]
        parent = self.parent  # type: Optional[Blockchain]
        forkpoint = self.forkpoint
        child_old_id = self.get_id()
        parent_old_id = parent.get_id()
        # swap files
        # child takes parent's name
        # parent's new name will be something new (not child's old name)
        self.assert_headers_file_available(self.path())
        child_old_name = self.path()
        with open(self.path(), 'rb') as f:
            my_data = f.read()
        self.assert_headers_file_available(parent.path())
        assert forkpoint > parent.forkpoint, (f"forkpoint of parent chain ({parent.forkpoint}) "
                                              f"should be at lower height than children's ({forkpoint})")
        offset = self.get_offset(parent.forkpoint, forkpoint)
        with open(parent.path(), 'rb') as f:
            f.seek(offset)
            parent_data = f.read()
        self.write(parent_data, 0)
        parent.write(my_data, offset)
        # swap parameters
        self.parent, parent.parent = parent.parent, self  # type: Optional[Blockchain], Optional[Blockchain]
        self.forkpoint, parent.forkpoint = parent.forkpoint, self.forkpoint
        self._forkpoint_hash, parent._forkpoint_hash = parent._forkpoint_hash, hash_raw_header(bh2u(parent_data[:get_header_size(self.parent.height())]))
        self._prev_hash, parent._prev_hash = parent._prev_hash, self._prev_hash
        # parent's new name
        os.replace(child_old_name, parent.path())
        self.update_size(self.size())
        parent.update_size(self.parent.size())
        # update pointers
        blockchains.pop(child_old_id, None)
        blockchains.pop(parent_old_id, None)
        blockchains[self.get_id()] = self
        blockchains[parent.get_id()] = parent
        return True

    def get_id(self) -> str:
        return self._forkpoint_hash

    def assert_headers_file_available(self, path):
        if os.path.exists(path):
            return
        elif not os.path.exists(util.get_headers_dir(self.config)):
            raise FileNotFoundError('Electrum headers_dir does not exist. Was it deleted while running?')
        else:
            raise FileNotFoundError('Cannot find headers file but headers_dir is there. Should be at {}'.format(path))

    @with_lock
    def get_offset(self, checkpoint, height):
        # Pre-Fork
        prb = 0
        if not is_post_equihash_fork(height):
            prb = height - checkpoint
        else:
            prb = constants.net.EQUIHASH_FORK_HEIGHT
        
        # Equihash Fork
        peb = 0
        if is_post_equihash_fork(height):
            peb = height - max(checkpoint, constants.net.EQUIHASH_FORK_HEIGHT)

        offset = (prb * HDR_LEN) \
            + (peb * get_header_size(constants.net.EQUIHASH_FORK_HEIGHT))
        return offset

    @with_lock
    def write(self, data: bytes, offset: int, truncate: bool=True) -> None:
        filename = self.path()
        current_offset = self.get_offset(self.forkpoint, self.height())
        with open(filename, 'rb+') as f:
            if truncate and offset != current_offset:
                f.seek(offset)
                f.truncate()
            f.seek(offset)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        self.update_size(self.size())

    @with_lock
    def save_header(self, header: dict) -> None:
        height = header.get('block_height')
        delta = height - self.forkpoint
        ser_header = serialize_header(header)

        offset = self.get_offset(self.forkpoint, height)
        header_size = get_header_size(height)
        data = bfh(ser_header)
        length = len(data)

        assert delta == self.size()
        assert length == header_size
        self.write(data, offset)

        self.swap_with_parent()

    @with_lock
    def read_header(self, height: int) -> Optional[dict]:
        if height < 0:
            return
        if height < self.forkpoint:
            return self.parent().read_header(height)
        if height > self.height():
            return
        offset = self.get_offset(self.forkpoint, height)
        header_size = get_header_size(height)
        name = self.path()

        if os.path.exists(name):
            with open(name, 'rb') as f:
                f.seek(offset)
                h = f.read(header_size)
                if len(h) < header_size:
                    raise Exception('Expected to read a full header. This was only {} bytes'.format(len(h)))
        elif not os.path.exists(util.get_headers_dir(self.config)):
            raise Exception('Electrum datadir does not exist. Was it deleted while running?')
        else:
            raise Exception('Cannot find headers file but datadir is there. Should be at {}'.format(name))
        if h == bytes([0])*header_size:
            return None
        return deserialize_header(h, height)

    def header_at_tip(self) -> Optional[dict]:
        """Return latest header."""
        height = self.height()
        return self.read_header(height)

    def get_hash(self, height: int) -> str:
        def is_height_checkpoint():
            within_cp_range = height <= constants.net.max_checkpoint()
            at_chunk_boundary = (height+1) % 200 == 0
            return within_cp_range and at_chunk_boundary

        if height == -1:
            return '0000000000000000000000000000000000000000000000000000000000000000'
        elif height == 0:
            return constants.net.GENESIS
        elif is_height_checkpoint():
            index = height // 200
            h, t = self.checkpoints[index]
            return h
        elif height < len(self.checkpoints) * CHUNK_LEN - TARGET_CALC_BLOCKS:
            assert (height+1) % CHUNK_LEN == 0, height
            index = height // CHUNK_LEN
            h, t, extra_headers = self.checkpoints[index]
            return h
        else:
            header = self.read_header(height)
            if header is None:
                raise MissingHeader(height)
            return hash_header(header)

    def get_target(self, height: int, chunk_headers=None) -> int:
        if chunk_headers is None or chunk_headers['empty']:
            chunk_empty = True
        else:
            chunk_empty = False
            min_height = chunk_headers['min_height']
            max_height = chunk_headers['max_height']
        if height <= POW_AVERAGING_WINDOW:
            return MAX_TARGET
        if (height > EH_EPOCH_1_END - POW_AVERAGING_WINDOW and height <= EH_EPOCH_1_END):
            return MIN_TARGET
        
        # LWMA fork
        if(height >= LWMA_FORK_BLOCK):
            T = POW_TARGET_SPACING
            N = ZAWY_LWMA3_AVERAGING_WINDOW
            k = int(N * (N + 1) * T / 2)

            previousTimestamp = 0
            t = 0
            j = 0
            sumTarget = 0

            if (height < N):
                return MAX_TARGET

            if(not chunk_empty and min_height <= height - N - 1 <= max_height):
                previousTimestamp = chunk_headers[height - N - 1].get('timestamp')
            else:
                previousTimestamp = self.read_header(height - N - 1).get('timestamp')
            

            for h in range(height - N, height):
                # self.print_error('height ', height)
                header = self.read_header(h)
                if not header and not chunk_empty \
                    and min_height <= h <= max_height:
                        header = chunk_headers[h]
                if not header:
                    raise Exception("Can not read header at height %s" % h)
                    
                if header.get('timestamp') > previousTimestamp:
                    thisTimestamp = header.get('timestamp')
                else:
                    thisTimestamp = previousTimestamp + 1
                solvetime = min(6 * T, thisTimestamp - previousTimestamp)
                previousTimestamp = thisTimestamp
                j += 1
                t += solvetime * j # Weighted solvetime sum.
                sumTarget += (self.bits_to_target(header.get('bits')) // (k * N))

                if(h == height - 1):
                    previousDiff = self.bits_to_target(header.get('bits'))

            nextTarget = t * sumTarget

            if (nextTarget > (previousDiff * 150) / 100):
                nextTarget = (previousDiff * 150) / 100
            if ((previousDiff * 67) / 100 > nextTarget):
                nextTarget = (previousDiff * 67)/100
            if (nextTarget > MAX_TARGET):
                nextTarget = MAX_TARGET
            
            return nextTarget

        # Digishield
        else:
            height_range = range(max(0, height - POW_AVERAGING_WINDOW),
                                max(1, height))
            mean_target = 0
            for h in height_range:
                header = self.read_header(h)
                if not header and not chunk_empty \
                    and min_height <= h <= max_height:
                        header = chunk_headers[h]
                if not header:
                    raise Exception("Can not read header at height %s" % h)
                mean_target += self.bits_to_target(header.get('bits'))
            mean_target //= POW_AVERAGING_WINDOW
            actual_timespan = self.get_median_time(height, chunk_headers) - \
                self.get_median_time(height - POW_AVERAGING_WINDOW, chunk_headers)
            actual_timespan = AVERAGING_WINDOW_TIMESPAN + \
                int((actual_timespan - AVERAGING_WINDOW_TIMESPAN) / \
                    POW_DAMPING_FACTOR)
            if actual_timespan < MIN_ACTUAL_TIMESPAN:
                actual_timespan = MIN_ACTUAL_TIMESPAN
            elif actual_timespan > MAX_ACTUAL_TIMESPAN:
                actual_timespan = MAX_ACTUAL_TIMESPAN

            next_target = mean_target // AVERAGING_WINDOW_TIMESPAN * actual_timespan

            if next_target > MAX_TARGET:
                next_target = MAX_TARGET

            return next_target

    def get_median_time(self, height, chunk_headers=None):

        if chunk_headers is None or chunk_headers['empty']:
            chunk_empty = True
        else:
            chunk_empty = False
            min_height = chunk_headers['min_height']
            max_height = chunk_headers['max_height']

        height_range = range(max(0, height - POW_MEDIAN_BLOCK_SPAN),
                             max(1, height))
        median = []
        for h in height_range:
            header = self.read_header(h)
            if not header and not chunk_empty \
                and min_height <= h <= max_height:
                    header = chunk_headers[h]
            if not header:
                raise Exception("Can not read header at height %s" % h)
            median.append(header.get('timestamp'))

        median.sort()
        return median[len(median)//2]

    @classmethod
    def bits_to_target(cls, bits: int) -> int:
        bitsN = (bits >> 24) & 0xff
        if not (0x03 <= bitsN <= 0x1f):
            raise Exception("First part of bits should be in [0x03, 0x1f]")
        bitsBase = bits & 0xffffff
        if not (0x8000 <= bitsBase <= 0x7fffff):
            raise Exception("Second part of bits should be in [0x8000, 0x7fffff]")
        return bitsBase << (8 * (bitsN-3))

    @classmethod
    def target_to_bits(cls, target: int) -> int:
        c = ("%064x" % target)[2:]
        while c[:2] == '00' and len(c) > 6:
            c = c[2:]
        bitsN, bitsBase = len(c) // 2, int.from_bytes(bfh(c[:6]), byteorder='big')
        if bitsBase >= 0x800000:
            bitsN += 1
            bitsBase >>= 8
        return bitsN << 24 | bitsBase

    def can_connect(self, header: dict, check_height: bool=True) -> bool:
        if header is None:
            return False
        height = header['block_height']
        if check_height and self.height() != height - 1:
            return False
        if height == 0:
            return hash_header(header) == constants.net.GENESIS
        try:
            prev_hash = self.get_hash(height - 1)
        except:
            return False
        if prev_hash != header.get('prev_block_hash'):
            return False
        try:
            target = self.get_target(height)
        except MissingHeader:
            return False
        try:
            self.verify_header(header, prev_hash, target)
        except BaseException as e:
            return False
        return True

    def connect_chunk(self, idx: int, hexdata: str) -> bool:
        assert idx >= 0, idx
        try:
            data = bfh(hexdata)
            self.verify_chunk(idx, data)
            self.save_chunk(idx, data)
            return True
        except BaseException as e:
            return False

    def get_checkpoints(self):
        # for each chunk, store the hash of the last block and the target after the chunk
        cp = []
        n = self.height() // 200
        for index in range(n):
            h = self.get_hash((index+1) * 200 -1)
            target = self.get_target(index)
            cp.append((h, target))
        return cp


def check_header(header: dict) -> Optional[Blockchain]:
    if type(header) is not dict:
        return None
    with blockchains_lock: chains = list(blockchains.values())
    for b in chains:
        if b.check_header(header):
            return b
    return None


def can_connect(header: dict) -> Optional[Blockchain]:
    with blockchains_lock: chains = list(blockchains.values())
    for b in chains:
        if b.can_connect(header):
            return b
    return None
