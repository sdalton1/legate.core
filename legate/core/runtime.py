# Copyright 2021 NVIDIA Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import gc
import math
import struct
import weakref
from collections import deque
from functools import partial, reduce

import numpy as np

from legion_cffi import ffi  # Make sure we only have one ffi instance
from legion_top import cleanup_items, top_level

from .context import Context
from .corelib import CoreLib
from .legion import (
    AffineTransform,
    Attach,
    Detach,
    FieldSpace,
    Future,
    IndexPartition,
    IndexSpace,
    InlineMapping,
    PartitionByRestriction,
    Point,
    Rect,
    Region,
    Transform,
    legate_task_postamble,
    legate_task_preamble,
    legion,
)


# A Field holds a reference to a field in a region tree
# that can be used by many different RegionField objects
class Field(object):
    __slots__ = [
        "runtime",
        "region",
        "field_id",
        "dtype",
        "shape",
        "partition",
        "own",
    ]

    def __init__(self, runtime, region, field_id, dtype, shape, own=True):
        self.runtime = runtime
        self.region = region
        self.field_id = field_id
        self.dtype = dtype
        self.shape = shape
        self.partition = None
        self.own = own

    def __del__(self):
        if self.own:
            # Return our field back to the runtime
            self.runtime.free_field(
                self.region,
                self.field_id,
                self.dtype,
                self.shape,
                self.partition,
            )


_sizeof_int = ffi.sizeof("int")
_sizeof_size_t = ffi.sizeof("size_t")
assert _sizeof_size_t == 4 or _sizeof_size_t == 8


# A helper class for doing field management with control replication
class FieldMatch(object):
    __slots__ = ["manager", "fields", "input", "output", "future"]

    def __init__(self, manager, fields):
        self.manager = manager
        self.fields = fields
        # Allocate arrays of ints that are twice as long as fields since
        # our values will be 'field_id,tree_id' pairs
        if len(fields) > 0:
            alloc_string = "int[" + str(2 * len(fields)) + "]"
            self.input = ffi.new(alloc_string)
            self.output = ffi.new(alloc_string)
            # Fill in the input buffer with our data
            for idx in range(len(fields)):
                region, field_id = fields[idx]
                self.input[2 * idx] = region.handle.tree_id
                self.input[2 * idx + 1] = field_id
        else:
            self.input = ffi.NULL
            self.output = ffi.NULL
        self.future = None

    def launch(self, runtime, context):
        assert self.future is None
        self.future = Future(
            legion.legion_context_consensus_match(
                runtime,
                context,
                self.input,
                self.output,
                len(self.fields),
                2 * _sizeof_int,
            )
        )
        return self.future

    def update_free_fields(self):
        # If we know there are no fields then we can be done early
        if len(self.fields) == 0:
            return
        # Wait for the future to be ready
        if not self.future.is_ready():
            self.future.wait()
        # Get the size of the buffer in the returned
        if _sizeof_size_t == 4:
            num_fields = struct.unpack_from("I", self.future.get_buffer(4))[0]
        else:
            num_fields = struct.unpack_from("Q", self.future.get_buffer(8))[0]
        assert num_fields <= len(self.fields)
        if num_fields > 0:
            # Put all the returned fields onto the ordered queue in the order
            # that they are in this list since we know
            ordered_fields = [None] * num_fields
            for region, field_id in self.fields:
                found = False
                for idx in range(num_fields):
                    if self.output[2 * idx] != region.handle.tree_id:
                        continue
                    if self.output[2 * idx + 1] != field_id:
                        continue
                    assert ordered_fields[idx] is None
                    ordered_fields[idx] = (region, field_id)
                    found = True
                    break
                if not found:
                    # Not found so put it back int the unordered queue
                    self.manager.free_field(region, field_id, ordered=False)
            # Notice that we do this in the order of the list which is the
            # same order as they were in the array returned by the match
            for region, field_id in ordered_fields:
                self.manager.free_field(region, field_id, ordered=True)
        else:
            # No fields on all shards so put all our fields back into
            # the unorered queue
            for region, field_id in self.fields:
                self.manager.free_field(region, field_id, ordered=False)


# This class manages the allocation and reuse of fields
class FieldManager(object):
    __slots__ = [
        "runtime",
        "shape",
        "dtype",
        "free_fields",
        "freed_fields",
        "matches",
        "match_counter",
        "match_frequency",
        "top_regions",
        "initial_future",
        "fill_space",
        "tile_shape",
    ]

    def __init__(self, runtime, shape, dtype):
        self.runtime = runtime
        self.shape = shape
        self.dtype = dtype
        # This is a sanitized list of (region,field_id) pairs that is
        # guaranteed to be ordered across all the shards even with
        # control replication
        self.free_fields = deque()
        # This is an unsanitized list of (region,field_id) pairs which is not
        # guaranteed to be ordered across all shards with control replication
        self.freed_fields = list()
        # A list of match operations that have been issued and for which
        # we are waiting for values to come back
        self.matches = deque()
        self.match_counter = 0
        # Figure out how big our match frequency is based on our size
        volume = reduce(lambda x, y: x * y, self.shape)
        size = volume * self.dtype.itemsize
        if size > runtime.max_field_reuse_size:
            # Figure out the ratio our size to the max reuse size (round up)
            ratio = (
                size + runtime.max_field_reuse_size - 1
            ) // runtime.max_field_reuse_size
            assert ratio >= 1
            # Scale the frequency by the ratio, but make it at least 1
            self.match_frequency = (
                runtime.max_field_reuse_frequency + ratio - 1
            ) // ratio
        else:
            self.match_frequency = runtime.max_field_reuse_frequency
        self.top_regions = list()  # list of top-level regions with this shape
        self.initial_future = None
        self.fill_space = None
        self.tile_shape = None

    def destroy(self):
        while self.top_regions:
            region = self.top_regions.pop()
            region.destroy()
        self.free_fields = None
        self.freed_fields = None
        self.initial_future = None
        self.fill_space = None

    def allocate_field(self):
        # Increment our match counter
        self.match_counter += 1
        # If the match counter equals our match frequency then do an exchange
        if self.match_counter == self.match_frequency:
            # This is where the rubber meets the road between control
            # replication and garbage collection. We need to see if there
            # are any freed fields that are shared across all the shards.
            # We have to test this deterministically no matter what even
            # if we don't have any fields to offer ourselves because this
            # is a collective with other shards. If we have any we can use
            # the first one and put the remainder on our free fields list
            # so that we can reuse them later. If there aren't any then
            # all the shards will go allocate a new field.
            local_freed_fields = self.freed_fields
            # The match now owns our freed fields so make a new list
            # Have to do this before dispatching the match
            self.freed_fields = list()
            match = FieldMatch(self, local_freed_fields)
            # Dispatch the match
            self.runtime.dispatch(match)
            # Put it on the deque of outstanding matches
            self.matches.append(match)
            # Reset the match counter back to 0
            self.match_counter = 0
        # First, if we have a free field then we know everyone has one of those
        if len(self.free_fields) > 0:
            return self.free_fields.popleft()
        # If we don't have any free fields then see if we have a pending match
        # outstanding that we can now add to our free fields and use
        while len(self.matches) > 0:
            match = self.matches.popleft()
            match.update_free_fields()
            # Check again to see if we have any free fields
            if len(self.free_fields) > 0:
                return self.free_fields.popleft()
        # Still don't have a field
        # Scan through looking for a free field of the right type
        for reg in self.top_regions:
            # Check to see if we've maxed out the fields for this region
            # Note that this next block ensures that we go
            # through all the fields in a region before reusing
            # any of them. This is important for avoiding false
            # aliasing in the generation of fields
            if reg.field_space.has_space:
                region = reg
                field_id = reg.field_space.allocate_field(self.dtype)
                return region, field_id
        # If we make it here then we need to make a new region
        index_space = self.runtime.find_or_create_index_space(self.shape)
        field_space = self.runtime.find_or_create_field_space(self.dtype)
        handle = legion.legion_logical_region_create(
            self.runtime.legion_runtime,
            self.runtime.legion_context,
            index_space.handle,
            field_space.handle,
            True,
        )
        region = Region(
            self.runtime.legion_context,
            self.runtime.legion_runtime,
            index_space,
            field_space,
            handle,
        )
        self.top_regions.append(region)
        field_id = None
        # See if this is a new fields space or not
        if len(field_space) > 0:
            # This field space has been used already, grab the first
            # field for ourselves and put any other ones on the free list
            for fid in field_space.fields.keys():
                if field_id is None:
                    field_id = fid
                else:
                    self.free_fields.append((region, fid))
        else:
            field_id = field_space.allocate_field(self.dtype)
        return region, field_id

    def free_field(self, region, field_id, ordered=False):
        if ordered:
            # Issue a fill to clear the field for re-use and enable the
            # Legion garbage collector to reclaim any physical instances
            # We'll disable this for now until we see evidence that we
            # actually need the Legion garbage collector
            # if self.initial_future is None:
            #    value = np.array(0, dtype=dtype)
            #    self.initial_future = self.runtime.create_future(value.data, value.nbytes) # noqa E501
            #    self.fill_space = self.runtime.compute_parallel_launch_space_by_shape( # noqa E501
            #                                                                    self.shape) # noqa E501
            #    if self.fill_space is not None:
            #        self.tile_shape = self.runtime.compute_tile_shape(self.shape, # noqa E501
            #                                                          self.fill_space) # noqa E501
            # if self.fill_space is not None and self.tile_shape in region.tile_partitions: # noqa E501
            #    partition = region.tile_partitions[self.tile_key]
            #    fill = IndexFill(partition, 0, region, field_id, self.initial_future, # noqa E501
            #                     mapper=self.runtime.mapper_id)
            # else:
            #    # We better be the top-level region
            #    fill = Fill(region, region, field_id, self.initial_future,
            #                mapper=self.runtime.mapper_id)
            # self.runtime.dispatch(fill)
            if self.free_fields is not None:
                self.free_fields.append((region, field_id))
        else:  # Put this on the unordered list
            if self.freed_fields is not None:
                self.freed_fields.append((region, field_id))


def _find_or_create_partition(
    runtime, region, color_shape, tile_shape, offset, transform, complete=True
):
    # Compute the extent and transform for this partition operation
    lo = (0,) * len(tile_shape)
    # Legion is inclusive so map down
    hi = tuple(map(lambda x: (x - 1), tile_shape))
    if offset is not None:
        assert len(offset) == len(tile_shape)
        lo = tuple(map(lambda x, y: (x + y), lo, offset))
        hi = tuple(map(lambda x, y: (x + y), hi, offset))
    # Construct the transform to use based on the color space
    tile_transform = Transform(len(tile_shape), len(tile_shape))
    for idx, tile in enumerate(tile_shape):
        tile_transform.trans[idx, idx] = tile
    # If we have a translation back to the region space we need to apply that
    if transform is not None:
        # Transform the extent points into the region space
        lo = transform.apply(lo)
        hi = transform.apply(hi)
        # Compose the transform from the color space into our shape space with
        # the transform from our shape space to region space
        tile_transform = tile_transform.compose(transform)
    # Now that we have the points in the global coordinate space we can build
    # the domain for the extent
    extent = Rect(hi, lo, exclusive=False)
    # Check to see if we already made a partition like this
    if region.index_space.children:
        color_lo = Point((0,) * len(color_shape), dim=len(color_shape))
        color_hi = Point(dim=len(color_shape))
        for idx in range(color_hi.dim):
            color_hi[idx] = color_shape[idx] - 1
        for part in region.index_space.children:
            if not isinstance(part.functor, PartitionByRestriction):
                continue
            if part.functor.transform != tile_transform:
                continue
            if part.functor.extent != extent:
                continue
            # Lastly check that the index space domains match
            color_bounds = part.color_space.get_bounds()
            if color_bounds.lo != color_lo or color_bounds.hi != color_hi:
                continue
            # Get the corresponding logical partition
            result = region.get_child(part)
            # Annotate it with our meta-data
            if not hasattr(result, "color_shape"):
                result.color_shape = color_shape
                result.tile_shape = tile_shape
                result.tile_offset = offset
            return result
    color_space = runtime.find_or_create_index_space(color_shape)
    functor = PartitionByRestriction(tile_transform, extent)
    index_partition = IndexPartition(
        runtime.legion_context,
        runtime.legion_runtime,
        region.index_space,
        color_space,
        functor,
        kind=legion.LEGION_DISJOINT_COMPLETE_KIND
        if complete
        else legion.LEGION_DISJOINT_INCOMPLETE_KIND,
        keep=True,  # export this partition functor to other libraries
    )
    partition = region.get_child(index_partition)
    partition.color_shape = color_shape
    partition.tile_shape = tile_shape
    partition.tile_offset = offset
    return partition


# A region field holds a reference to a field in a logical region
class RegionField(object):
    def __init__(
        self,
        runtime,
        region,
        field,
        shape,
        parent=None,
        transform=None,
        dim_map=None,
        key=None,
        view=None,
    ):
        self.runtime = runtime
        self.attachment_manager = runtime.attachment_manager
        self.context = runtime.core_context
        self.region = region
        self.field = field
        self.shape = shape
        self.parent = parent
        self.transform = transform
        self.dim_map = dim_map
        self.key = key
        self.key_partition = None  # The key partition for this region field
        self.subviews = None  # RegionField subviews of this region field
        self.view = view  # The view slice tuple used to make this region field
        self.launch_space = None  # Parallel launch space for this region_field
        self.shard_function = 0  # Default to no shard function
        self.shard_space = None  # Sharding space for launches
        self.shard_point = None  # Tile point we overlap with in root
        self.attach_array = None  # Numpy array that we attached to this field
        self.numpy_array = (
            None  # Numpy array that we returned for the application
        )
        self.interface = None  # Numpy array interface
        self.physical_region = None  # Physical region for attach
        self.physical_region_refs = 0
        self.physical_region_mapped = False

    def __del__(self):
        if self.attach_array is not None:
            self.detach_numpy_array(unordered=True, defer=True)

    def has_parallel_launch_space(self):
        return self.launch_space is not None

    def compute_parallel_launch_space(self):
        # See if we computed it already
        if self.launch_space == ():
            return None
        if self.launch_space is not None:
            return self.launch_space
        if self.parent is not None:
            key_partition, _, __ = self.find_or_create_key_partition()
            if key_partition is None:
                self.launch_space = ()
            else:
                self.launch_space = key_partition.color_shape
        else:  # top-level region so just do the natural partitioning
            self.launch_space = self.runtime.compute_parallel_launch_space_by_shape(  # noqa E501
                self.shape
            )
            if self.launch_space is None:
                self.launch_space = ()
        if self.launch_space == ():
            return None
        return self.launch_space

    def find_point_sharding(self):
        # By the time we call this we should have a launch space
        # even if it is an empty one
        assert self.launch_space is not None
        return self.shard_point, self.shard_function, self.shard_space

    def set_key_partition(self, part, shardfn=None, shardsp=None):
        assert part.parent == self.region
        self.launch_space = part.color_shape
        self.key_partition = part
        self.shard_function = 0
        self.shard_space = shardsp

    def find_or_create_key_partition(self):
        if self.key_partition is not None:
            return self.key_partition, self.shard_function, self.shard_space
        # We already tried to compute it and did not have one so we're done
        if self.launch_space == ():
            return None, None, None
        if self.parent is not None:
            # Figure out how many tiles we overlap with of the root
            root = self.parent
            while root.parent is not None:
                root = root.parent
            root_key, rootfn, rootsp = root.find_or_create_key_partition()
            if root_key is None:
                self.launch_space = ()
                return None, None, None
            # Project our bounds through the transform into the
            # root coordinate space to get our bounds in the root
            # coordinate space
            lo = np.zeros((len(self.shape),), dtype=np.int64)
            hi = np.array(self.shape, dtype=np.int64) - 1
            if self.transform:
                lo = self.transform.apply(lo)
                hi = self.transform.apply(hi)
            # Compute the lower bound tile and upper bound tile
            assert len(lo) == len(root_key.tile_shape)
            color_lo = tuple(map(lambda x, y: x // y, lo, root_key.tile_shape))
            color_hi = tuple(map(lambda x, y: x // y, hi, root_key.tile_shape))
            color_tile = root_key.tile_shape
            if self.transform:
                # Check to see if this transform is invertible
                # If it is then we'll reuse the key partition of the
                # root in order to guide how we do the partitioning
                # for this view to maximimize locality. If the transform
                # is not invertible then we'll fall back to doing the
                # standard mapping of the index space
                invertible = True
                for m in range(len(root.shape)):
                    nonzero = False
                    for n in range(len(self.shape)):
                        if self.transform.trans[m, n] != 0:
                            if nonzero:
                                invertible = False
                                break
                            if self.transform.trans[m, n] != 1:
                                invertible = False
                                break
                            nonzero = True
                    if not invertible:
                        break
                if not invertible:
                    # Not invertible so fall back to the standard case
                    launch_space = (
                        self.runtime.compute_parallel_launch_space_by_shape(
                            self.shape
                        )
                    )
                    if launch_space == ():
                        return None, None
                    tile_shape = self.runtime.compute_tile_shape(
                        self.shape, launch_space
                    )
                    self.key_partition = _find_or_create_partition(
                        self.runtime,
                        self.region,
                        launch_space,
                        tile_shape,
                        offset=(0,) * len(launch_space),
                        transform=self.transform,
                    )
                    self.shard_function = 0
                    return (
                        self.key_partition,
                        self.shard_function,
                        self.shard_space,
                    )
                # We're invertible so do the standard inversion
                inverse = np.transpose(self.transform.trans)
                # We need to make a make a special sharding functor here that
                # projects the points in our launch space back into the space
                # of the root partitions sharding space
                # First construct the affine mapping for points in our launch
                # space back into the launch space of the root
                # This is the special case where we have a special shard
                # function and sharding space that is different than our normal
                # launch space because it's a subset of the root's launch space
                launch_transform = AffineTransform(
                    len(root.shape), len(self.shape), False
                )
                launch_transform.trans = self.transform.trans
                launch_transform.offset = color_lo
                self.shard_function = 0
                tile_offset = np.zeros((len(self.shape),), dtype=np.int64)
                for n in range(len(self.shape)):
                    nonzero = False
                    for m in range(len(root.shape)):
                        if inverse[n, m] == 0:
                            continue
                        nonzero = True
                        break
                    if not nonzero:
                        tile_offset[n] = 1
                color_lo = tuple((inverse @ color_lo).flatten())
                color_hi = tuple((inverse @ color_hi).flatten())
                color_tile = tuple(
                    (inverse @ color_tile).flatten() + tile_offset
                )
                # Reset lo and hi back to our space
                lo = np.zeros((len(self.shape),), dtype=np.int64)
                hi = np.array(self.shape, dtype=np.int64) - 1
            else:
                # If there is no transform then can just use the root function
                self.shard_function = rootfn
            self.shard_space = root_key.index_partition.color_space
            # Launch space is how many tiles we have in each dimension
            color_shape = tuple(
                map(lambda x, y: (x - y) + 1, color_hi, color_lo)
            )
            # Check to see if they are all one, if so then we don't even need
            # to bother with making the partition
            volume = reduce(lambda x, y: x * y, color_shape)
            assert volume > 0
            if volume == 1:
                self.launch_space = ()
                # We overlap with exactly one point in the root
                # Therefore just record this point
                self.shard_point = Point(color_lo)
                return None, None, None
            # Now compute the offset for the partitioning
            # This will shift the tile down if necessary to align with the
            # boundaries at the root while still covering all of our elements
            offset = tuple(
                map(
                    lambda x, y: 0 if (x % y) == 0 else ((x % y) - y),
                    lo,
                    color_tile,
                )
            )
            self.key_partition = _find_or_create_partition(
                self.runtime,
                self.region,
                color_shape,
                color_tile,
                offset,
                self.transform,
            )
        else:
            launch_space = self.compute_parallel_launch_space()
            if launch_space is None:
                return None, None, None
            tile_shape = self.runtime.compute_tile_shape(
                self.shape, launch_space
            )
            self.key_partition = _find_or_create_partition(
                self.runtime,
                self.region,
                launch_space,
                tile_shape,
                offset=(0,) * len(launch_space),
                transform=self.transform,
            )
            self.shard_function = 0
        return self.key_partition, self.shard_function, self.shard_space

    def find_or_create_congruent_partition(
        self, part, transform=None, offset=None
    ):
        if transform is not None:
            shape_transform = AffineTransform(transform.M, transform.N, False)
            shape_transform.trans = transform.trans.copy()
            shape_transform.offset = offset
            offset_transform = transform
            return self.find_or_create_partition(
                shape_transform.apply(part.color_shape),
                shape_transform.apply(part.tile_shape),
                offset_transform.apply(part.tile_offset),
            )
        else:
            assert len(self.shape) == len(part.color_shape)
            return self.find_or_create_partition(
                part.color_shape, part.tile_shape, part.tile_offset
            )

    def find_or_create_partition(
        self, launch_space, tile_shape=None, offset=None
    ):
        # Compute a tile shape based on our shape
        if tile_shape is None:
            tile_shape = self.runtime.compute_tile_shape(
                self.shape, launch_space
            )
        if offset is None:
            offset = (0,) * len(launch_space)
        # Tile shape should have the same number of dimensions as our shape
        assert len(launch_space) == len(self.shape)
        assert len(tile_shape) == len(self.shape)
        assert len(offset) == len(self.shape)
        # Do a quick check to see if this is congruent to our key partition
        if (
            self.key_partition is not None
            and launch_space == self.key_partition.color_shape
            and tile_shape == self.key_partition.tile_shape
            and offset == self.key_partition.tile_offset
        ):
            return self.key_partition
        # Continue this process on the region object, to ensure any created
        # partitions are shared between RegionField objects referring to the
        # same region
        return _find_or_create_partition(
            self.runtime,
            self.region,
            launch_space,
            tile_shape,
            offset,
            self.transform,
        )

    def find_or_create_indirect_partition(self, launch_space):
        assert len(launch_space) != len(self.shape)
        # If there is a mismatch in the number of dimensions then we need
        # to compute a partition and projection functor that can transform
        # the points into a partition that makes sense
        raise NotImplementedError("need support for indirect partitioning")

    def attach_numpy_array(self, numpy_array, share=False):
        assert self.parent is None
        assert isinstance(numpy_array, np.ndarray)
        # If we already have a numpy array attached
        # then we have to detach it first
        if self.attach_array is not None:
            if self.attach_array is numpy_array:
                return
            else:
                self.detach_numpy_array(unordered=False)
        # Now we can attach the new one and then do the acquire
        attach = Attach(
            self.region,
            self.field.field_id,
            numpy_array,
            mapper=self.context.mapper_id,
        )
        # If we're not sharing then there is no need to map or restrict the
        # attachment
        if not share:
            # No need for restriction for us
            attach.set_restricted(False)
            # No need for mapping in the restricted case
            attach.set_mapped(False)
        else:
            self.physical_region_mapped = True
        self.physical_region = self.runtime.dispatch(attach)
        # Due to the working of the Python interpreter's garbage collection
        # algorithm we make the detach operation for this now and register it
        # with the runtime so that we know that it won't be collected when the
        # RegionField object is collected
        detach = Detach(self.physical_region, flush=True)
        # Dangle these fields off here to prevent premature collection
        detach.field = self.field
        detach.array = numpy_array
        self.detach_key = self.attachment_manager.register_detachment(detach)
        # Add a reference here to prevent collection in for inline mapped cases
        assert self.physical_region_refs == 0
        # This reference will never be removed, we'll delete the
        # physical region once the object is deleted
        self.physical_region_refs = 1
        self.attach_array = numpy_array
        if share:
            # If we're sharing this then we can also make this our numpy array
            self.numpy_array = weakref.ref(numpy_array)

    def detach_numpy_array(self, unordered, defer=False):
        assert self.parent is None
        assert self.attach_array is not None
        assert self.physical_region is not None
        detach = self.attachment_manager.remove_detachment(self.detach_key)
        detach.unordered = unordered
        self.attachment_manager.detach_array(
            self.attach_array, self.field, detach, defer
        )
        self.physical_region = None
        self.physical_region_mapped = False
        self.attach_array = None

    def get_inline_mapped_region(self, context):
        if self.parent is None:
            if self.physical_region is None:
                # We don't have a valid numpy array so we need to do an inline
                # mapping and then use the buffer to share the storage
                mapping = InlineMapping(
                    self.region,
                    self.field.field_id,
                    mapper=context.mapper_id,
                )
                self.physical_region = self.runtime.dispatch(mapping)
                self.physical_region_mapped = True
                # Wait until it is valid before returning
                self.physical_region.wait_until_valid()
            elif not self.physical_region_mapped:
                # If we have a physical region but it is not mapped then
                # we actually need to remap it, we do this by launching it
                self.runtime.dispatch(self.physical_region)
                self.physical_region_mapped = True
                # Wait until it is valid before returning
                self.physical_region.wait_until_valid()
            # Increment our ref count so we know when it can be collected
            self.physical_region_refs += 1
            return self.physical_region
        else:
            return self.parent.get_inline_mapped_region(context)

    def decrement_inline_mapped_ref_count(self):
        if self.parent is None:
            assert self.physical_region_refs > 0
            self.physical_region_refs -= 1
            if self.physical_region_refs == 0:
                self.runtime.unmap_region(self.physical_region)
                self.physical_region = None
                self.physical_region_mapped = False
        else:
            self.parent.decrement_inline_mapped_ref_count()

    def get_numpy_array(self, context=None):
        context = self.runtime.context if context is None else context

        # See if we still have a valid numpy array to use
        if self.numpy_array is not None:
            # Test the weak reference to see if it is still alive
            result = self.numpy_array()
            if result is not None:
                return result
        physical_region = self.get_inline_mapped_region(context)
        # We need a pointer to the physical allocation for this physical region
        dim = len(self.shape)
        # Build the accessor for this physical region
        if self.transform is not None:
            # We have a transform so build the accessor special with a
            # transform
            func = getattr(
                legion,
                "legion_physical_region_get_field_accessor_array_{}d_with_transform".format(  # noqa E501
                    dim
                ),
            )
            accessor = func(
                physical_region.handle,
                ffi.cast("legion_field_id_t", self.field.field_id),
                self.transform.raw(),
            )
        else:
            # No transfrom so we can do the normal thing
            func = getattr(
                legion,
                "legion_physical_region_get_field_accessor_array_{}d".format(
                    dim
                ),
            )
            accessor = func(
                physical_region.handle,
                ffi.cast("legion_field_id_t", self.field.field_id),
            )
        # Now that we've got our accessor we can get a pointer to the memory
        rect = ffi.new("legion_rect_{}d_t *".format(dim))
        for d in range(dim):
            rect[0].lo.x[d] = 0
            rect[0].hi.x[d] = self.shape[d] - 1  # inclusive
        subrect = ffi.new("legion_rect_{}d_t *".format(dim))
        offsets = ffi.new("legion_byte_offset_t[]", dim)
        func = getattr(
            legion, "legion_accessor_array_{}d_raw_rect_ptr".format(dim)
        )
        base_ptr = func(accessor, rect[0], subrect, offsets)
        assert base_ptr is not None
        # Check that the subrect is the same as in the in rect
        for d in range(dim):
            assert rect[0].lo.x[d] == subrect[0].lo.x[d]
            assert rect[0].hi.x[d] == subrect[0].hi.x[d]
        shape = tuple(rect.hi.x[i] - rect.lo.x[i] + 1 for i in range(dim))
        strides = tuple(offsets[i].offset for i in range(dim))
        # Numpy doesn't know about CFFI pointers, so we have to cast
        # this to a Python long before we can hand it off to Numpy.
        base_ptr = int(ffi.cast("size_t", base_ptr))
        initializer = _RegionNdarray(
            shape, self.field.dtype, base_ptr, strides, False
        )
        array = np.asarray(initializer)

        # This will be the unmap call that will be invoked once the weakref is
        # removed
        # We will use it to unmap the inline mapping that was performed
        def decrement(region_field, ref):
            region_field.decrement_inline_mapped_ref_count()

        # Curry bind arguments to the function
        callback = partial(decrement, self)
        # Save a weak reference to the array so we don't prevent collection
        self.numpy_array = weakref.ref(array, callback)
        return array


# This is a dummy object that is only used as an initializer for the
# RegionField object above. It is thrown away as soon as the
# RegionField is constructed.
class _RegionNdarray(object):
    __slots__ = ["__array_interface__"]

    def __init__(self, shape, field_type, base_ptr, strides, read_only):
        # See: https://docs.scipy.org/doc/numpy/reference/arrays.interface.html
        self.__array_interface__ = {
            "version": 3,
            "shape": shape,
            "typestr": field_type.str,
            "data": (base_ptr, read_only),
            "strides": strides,
        }


class Attachment(object):
    def __init__(self, ptr, extent, region, field):
        self.ptr = ptr
        self.extent = extent
        self.end = ptr + extent - 1
        self.count = 1
        self.region = region
        self.field = field

    def overlaps(self, other):
        return not (self.end < other.ptr or other.end < self.ptr)

    def equals(self, other):
        # Sufficient to check the pointer and extent
        # as they are used as a key for de-duplication
        return self.ptr == other.ptr and self.extent == other.extent

    def add_reference(self):
        self.count += 1

    def remove_reference(self):
        assert self.count > 0
        self.count += 1

    @property
    def collectible(self):
        return self.count == 0


class AttachmentManager(object):
    def __init__(self, runtime):
        self._runtime = runtime

        self._attachments = dict()

        self._next_detachment_key = 0
        self._registered_detachments = dict()
        self._deferred_detachments = list()
        self._pending_detachments = dict()

    def destroy(self):
        gc.collect()
        while self._deferred_detachments:
            self.perform_detachments()
            # Make sure progress is made on any of these operations
            self._runtime._progress_unordered_operations()
            gc.collect()
        # Always make sure we wait for any pending detachments to be done
        # so that we don't lose the references and make the GC unhappy
        gc.collect()
        while self._pending_detachments:
            self.prune_detachments()
            gc.collect()

        # Clean up our attachments so that they can be collected
        self._attachments = None
        self._registered_detachments = None
        self._deferred_detachments = None
        self._pending_detachments = None

    @staticmethod
    def attachment_key(array):
        return (int(array.ctypes.data), array.nbytes)

    def has_attachment(self, array):
        key = self.attachment_key(array)
        return key in self._attachments

    def attach_array(self, array, share):
        assert array.base is None or not isinstance(array.base, np.ndarray)
        # NumPy arrays are not hashable, so look up the pointer for the array
        # which should be unique for all root NumPy arrays
        key = self.attachment_key(array)
        if key not in self._attachments:
            region_field = self._runtime.allocate_field(
                array.shape, array.dtype
            )
            region_field.attach_numpy_array(array, share)
            attachment = Attachment(
                *key, region_field.region, region_field.field
            )

            # iterate over attachments and look for aliases which are bad
            for other in self._attachments.values():
                if other.overlaps(attachment):
                    assert not other.equals(attachment)
                    raise RuntimeError(
                        "Illegal aliased attachments not supported by Legate"
                    )

            self._attachments[key] = attachment
        else:
            attachment = self._attachments[key]
            attachment.add_reference()
            region = attachment.region
            field = attachment.field
            region_field = RegionField(
                self._runtime, region, field, array.shape
            )
        return region_field

    def remove_attachment(self, array):
        key = self.attachment_key(array)
        if key not in self._attachments:
            raise RuntimeError("Unable to find attachment to remove")
        attachment = self._attachments[key]
        attachment.remove_reference()
        if attachment.collectible:
            del self._attachments[key]

    def detach_array(self, array, field, detach, defer):
        if defer:
            # If we need to defer this until later do that now
            self._deferred_detachments.append((array, field, detach))
            return
        future = self._runtime.dispatch(detach)
        # Dangle a reference to the field off the future to prevent the
        # field from being recycled until the detach is done
        future.field_reference = field
        assert array.base is None
        # We also need to tell the core legate library that this array
        # is no longer attached
        self.remove_attachment(array)
        # If the future is already ready, then no need to track it
        if future.is_ready():
            return
        self._pending_detachments[future] = array

    def register_detachment(self, detach):
        key = self._next_detachment_key
        self._registered_detachments[key] = detach
        self._next_detachment_key += 1
        return key

    def remove_detachment(self, detach_key):
        detach = self._registered_detachments[detach_key]
        del self._registered_detachments[detach_key]
        return detach

    def perform_detachments(self):
        detachments = self._deferred_detachments
        self._deferred_detachments = list()
        for array, field, detach in detachments:
            self.detach_array(array, field, detach, defer=False)

    def prune_detachments(self):
        to_remove = []
        for future in self._pending_detachments.keys():
            if future.is_ready():
                to_remove.append(future)
        for future in to_remove:
            del self._pending_detachments[future]


class Runtime(object):
    def __init__(self):
        """
        This is a class that implements the Legate runtime.
        The Runtime object provides high-level APIs for Legate libraries
        to use services in the Legion runtime. The Runtime centralizes
        resource management for all the libraries so that they can
        focus on implementing their domain logic.
        """

        self.index_spaces = {}  # map shapes to index spaces
        self.field_spaces = {}  # map dtype to field spaces
        self.field_managers = {}  # map from (shape,dtype) to field managers

        self.destroyed = False
        self.max_field_reuse_size = 256
        self.max_field_reuse_frequency = 32
        self.num_pieces = 4
        self.launch_spaces = dict()
        self.min_shard_volume = 1

        factors = list()
        pieces = self.num_pieces
        while pieces % 2 == 0:
            factors.append(2)
            pieces = pieces // 2
        while pieces % 3 == 0:
            factors.append(3)
            pieces = pieces // 3
        while pieces % 5 == 0:
            factors.append(5)
            pieces = pieces // 5
        while pieces % 7 == 0:
            factors.append(7)
            pieces = pieces // 7
        while pieces % 11 == 0:
            factors.append(11)
            pieces = pieces // 11
        if pieces > 1:
            raise ValueError(
                "legate.numpy currently doesn't support processor "
                + "counts with large prime factors greater than 11"
            )
        self.piece_factors = list(reversed(factors))

        self._contexts = {}
        self._context_list = []
        self._core_context = None
        self._core_library = None
        self._empty_argmap = legion.legion_argument_map_create()

        # This list maintains outstanding operations from all legate libraries
        # to be dispatched. This list allows cross library introspection for
        # Legate operations.
        self._outstanding_ops = []

        try:
            self._legion_context = top_level.context[0]
        except AttributeError:
            pass

        # Record whether we need to run finalize tasks
        # Key off whether we are being loaded in a context or not
        try:
            # Do this first to detect if we're not in the top-level task
            self._legion_context = top_level.context[0]
            self._legion_runtime = legion.legion_runtime_get_runtime()
            legate_task_preamble(self._legion_runtime, self._legion_context)
            self._finalize_tasks = True
        except AttributeError:
            self._finalize_tasks = False
            self._legion_runtime = None
            self._legion_context = None

        self._attachment_manager = AttachmentManager(self)

    @property
    def legion_runtime(self):
        if self._legion_runtime is None:
            self._legion_runtime = legion.legion_runtime_get_runtime()
        return self._legion_runtime

    @property
    def legion_context(self):
        return self._legion_context

    @property
    def core_context(self):
        if self._core_context is None:
            self._core_context = self._contexts["legate.core"]
        return self._core_context

    @property
    def core_library(self):
        if self._core_library is None:
            self._core_library = self.core_context.library._lib
        return self._core_library

    @property
    def empty_argmap(self):
        return self._empty_argmap

    @property
    def attachment_manager(self):
        return self._attachment_manager

    def register_library(self, library):
        libname = library.get_name()
        if libname in self._contexts:
            raise RuntimeError(
                f"library {libname} has already been registered!"
            )
        # It's important that we load the library so that its constants
        # can be used for configuration.
        self.load_library(library)
        context = Context(self, library)
        self._contexts[libname] = context
        self._context_list.append(context)
        return context

    @staticmethod
    def load_library(library):
        shared_lib_path = library.get_shared_library()
        if shared_lib_path is not None:
            header = library.get_c_header()
            if header is not None:
                ffi.cdef(header)
            shared_lib = ffi.dlopen(shared_lib_path)
            library.initialize(shared_lib)
            callback_name = library.get_registration_callback()
            callback = getattr(shared_lib, callback_name)
            callback()
        else:
            library.initialize()

    def destroy(self):
        # Destroy all libraries. Note that we should do this
        # from the lastly added one to the first one
        for context in reversed(self._context_list):
            context.destroy()
        del self._contexts
        del self._context_list

        self._attachment_manager.destroy()

        # Remove references to our legion resources so they can be collected
        self.field_managers = None
        self.field_spaces = None
        self.index_spaces = None

        if self._finalize_tasks:
            # Run a gc and then end the legate task
            gc.collect()
            legate_task_postamble(self.legion_runtime, self.legion_context)

        self.destroyed = True

    def dispatch(self, op, redop=None):
        if redop:
            return op.launch(self.legion_runtime, self.legion_context, redop)
        else:
            return op.launch(self.legion_runtime, self.legion_context)

    def _progress_unordered_operations(self):
        legion.legion_context_progress_unordered_operations(
            self.legion_runtime, self.legion_context
        )

    def unmap_region(self, physical_region):
        physical_region.unmap(self.legion_runtime, self.legion_context)

    def get_projection(self, src_dim, tgt_dim, mask):
        proj_id = 0
        for dim, val in enumerate(mask):
            proj_id = proj_id | (int(val) << dim)
        proj_id = (proj_id << 8) | (src_dim << 4) | tgt_dim
        return self.core_context.get_projection_id(proj_id)

    def get_transpose(self, seq):
        dim = len(seq)
        # Convert the dimension sequence to a Lehmer code
        seq = np.array(seq)
        code = [(seq[idx + 1 :] < val).sum() for idx, val in enumerate(seq)]
        # Then convert the code into a factoradic number
        factoradic = sum(
            [
                val * math.factorial(dim - idx - 1)
                for idx, val in enumerate(code)
            ]
        )
        proj_id = (
            self.core_library.LEGATE_CORE_FIRST_TRANSPOSE_FUNCTOR
            | (factoradic << 4)
            | dim
        )
        return self.core_context.get_projection_id(proj_id)

    def allocate_field(self, shape, dtype):
        assert not self.destroyed
        region = None
        field_id = None
        # Regions all have fields of the same field type and shape
        key = (shape, dtype)
        # if we don't have a field manager yet then make one
        if key not in self.field_managers:
            self.field_managers[key] = FieldManager(self, shape, dtype)
        region, field_id = self.field_managers[key].allocate_field()
        field = Field(self, region, field_id, dtype, shape)
        return RegionField(self, region, field, shape)

    def free_field(self, region, field_id, dtype, shape, partition):
        # Have a guard here to make sure that we don't try to
        # do this after we have been destroyed
        if self.destroyed:
            return
        # Now save it in our data structure for free fields eligible for reuse
        key = (shape, dtype)
        if self.field_managers is not None:
            self.field_managers[key].free_field(region, field_id)

    def attach_array(self, array, share):
        return self._attachment_manager.attach_array(array, share)

    def has_attachment(self, array):
        return self._attachment_manager.has_attachment(array)

    def find_or_create_index_space(self, bounds):
        if bounds in self.index_spaces:
            return self.index_spaces[bounds]
        # Haven't seen this before so make it now
        rect = Rect(bounds)
        handle = legion.legion_index_space_create_domain(
            self.legion_runtime, self.legion_context, rect.raw()
        )
        result = IndexSpace(
            self.legion_context, self.legion_runtime, handle=handle
        )
        # Save this for the future
        self.index_spaces[bounds] = result
        return result

    def find_or_create_field_space(self, dtype):
        if dtype in self.field_spaces:
            return self.field_spaces[dtype]
        # Haven't seen this type before so make it now
        field_space = FieldSpace(self.legion_context, self.legion_runtime)
        self.field_spaces[dtype] = field_space
        return field_space

    def compute_parallel_launch_space_by_shape(self, shape):
        assert self.num_pieces > 0
        # Easy case if we only have one piece: no parallel launch space
        if self.num_pieces == 1:
            return None
        # If there is only one point or no points then we never do a parallel
        # launch
        all_ones_or_zeros = True
        for ext in shape:
            if ext > 1:
                all_ones_or_zeros = False
                break
            else:  # Better be a one or zero if we get here
                assert ext == 1 or ext == 0
        # If we only have one point then we never do parallel launches
        if all_ones_or_zeros:
            return None
        # Check to see if we already did the math
        if shape in self.launch_spaces:
            return self.launch_spaces[shape]
        # Prune out any dimensions that are 1
        temp_shape = ()
        temp_dims = ()
        volume = 1
        for dim in range(len(shape)):
            assert shape[dim] > 0
            if shape[dim] == 1:
                continue
            temp_shape = temp_shape + (shape[dim],)
            temp_dims = temp_dims + (dim,)
            volume *= shape[dim]
        # Figure out how many shards we can make with this array
        max_pieces = (
            volume + self.min_shard_volume - 1
        ) // self.min_shard_volume
        assert max_pieces > 0
        # If we can only make one piece return that now
        if max_pieces == 1:
            self.launch_spaces[shape] = None
            return None
        else:
            # TODO: a better heuristic here For now if we can make at least two
            # pieces then we will make N pieces
            max_pieces = self.num_pieces
        # Otherwise we need to compute it ourselves
        # First compute the N-th root of the number of pieces
        dims = len(temp_shape)
        temp_result = ()
        if dims == 0:
            # Project back onto the original number of dimensions
            result = ()
            for dim in range(len(shape)):
                result = result + (1,)
            return result
        elif dims == 1:
            # Easy case for one dimensional things
            temp_result = (min(temp_shape[0], max_pieces),)
        elif dims == 2:
            if volume < max_pieces:
                # TBD: Once the max_pieces heuristic is fixed, this should
                # never happen
                temp_result = temp_shape
            else:
                # Two dimensional so we can use square root to try and generate
                # as square a pieces as possible since most often we will be
                # doing matrix operations with these
                nx = temp_shape[0]
                ny = temp_shape[1]
                swap = nx > ny
                if swap:
                    temp = nx
                    nx = ny
                    ny = temp
                n = math.sqrt(float(max_pieces * nx) / float(ny))
                # Need to constraint n to be an integer with numpcs % n == 0
                # try rounding n both up and down
                n1 = int(math.floor(n + 1e-12))
                n1 = max(n1, 1)
                while max_pieces % n1 != 0:
                    n1 -= 1
                n2 = int(math.ceil(n - 1e-12))
                while max_pieces % n2 != 0:
                    n2 += 1
                # pick whichever of n1 and n2 gives blocks closest to square
                # i.e. gives the shortest long side
                side1 = max(nx // n1, ny // (max_pieces // n1))
                side2 = max(nx // n2, ny // (max_pieces // n2))
                px = n1 if side1 <= side2 else n2
                py = max_pieces // px
                # we need to trim launch space if it is larger than the
                # original shape in one of the dimensions (can happen in
                # testing)
                if swap:
                    temp_result = (
                        min(py, temp_shape[0]),
                        min(px, temp_shape[1]),
                    )
                else:
                    temp_result = (
                        min(px, temp_shape[0]),
                        min(py, temp_shape[1]),
                    )
        else:
            # For higher dimensions we care less about "square"-ness
            # and more about evenly dividing things, compute the prime
            # factors for our number of pieces and then round-robin
            # them onto the shape, with the goal being to keep the
            # last dimension >= 32 for good memory performance on the GPU
            temp_result = list()
            for dim in range(dims):
                temp_result.append(1)
            factor_prod = 1
            for factor in self.piece_factors:
                # Avoid exceeding the maximum number of pieces
                if factor * factor_prod > max_pieces:
                    break
                factor_prod *= factor
                remaining = tuple(
                    map(lambda s, r: (s + r - 1) // r, temp_shape, temp_result)
                )
                big_dim = remaining.index(max(remaining))
                if big_dim < len(temp_dims) - 1:
                    # Not the last dimension, so do it
                    temp_result[big_dim] *= factor
                else:
                    # Last dim so see if it still bigger than 32
                    if (
                        len(remaining) == 1
                        or remaining[big_dim] // factor >= 32
                    ):
                        # go ahead and do it
                        temp_result[big_dim] *= factor
                    else:
                        # Won't be see if we can do it with one of the other
                        # dimensions
                        big_dim = remaining.index(
                            max(remaining[0 : len(remaining) - 1])
                        )
                        if remaining[big_dim] // factor > 0:
                            temp_result[big_dim] *= factor
                        else:
                            # Fine just do it on the last dimension
                            temp_result[len(temp_dims) - 1] *= factor
        # Project back onto the original number of dimensions
        assert len(temp_result) == dims
        result = ()
        for dim in range(len(shape)):
            if dim in temp_dims:
                result = result + (temp_result[temp_dims.index(dim)],)
            else:
                result = result + (1,)
        # Save the result for later
        self.launch_spaces[shape] = result
        return result

    def compute_tile_shape(self, shape, launch_space):
        assert len(shape) == len(launch_space)
        # Over approximate the tiles so that the ends might be small
        return tuple(map(lambda x, y: (x + y - 1) // y, shape, launch_space))


_runtime = Runtime()
_runtime.register_library(CoreLib())


def _cleanup_legate_runtime():
    global _runtime
    _runtime.destroy()
    del _runtime
    gc.collect()


cleanup_items.append(_cleanup_legate_runtime)


def get_legion_runtime():
    return _runtime.legion_runtime


def get_legion_context():
    return _runtime.legion_context


def legate_add_library(library):
    _runtime.register_library(library)


def get_legate_runtime():
    return _runtime