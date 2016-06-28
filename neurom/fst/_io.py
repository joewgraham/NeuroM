# Copyright (c) 2015, Ecole Polytechnique Federale de Lausanne, Blue Brain Project
# All rights reserved.
#
# This file is part of NeuroM <https://github.com/BlueBrain/NeuroM>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     1. Redistributions of source code must retain the above copyright
#        notice, this list of conditions and the following disclaimer.
#     2. Redistributions in binary form must reproduce the above copyright
#        notice, this list of conditions and the following disclaimer in the
#        documentation and/or other materials provided with the distribution.
#     3. Neither the name of the copyright holder nor the names of
#        its contributors may be used to endorse or promote products
#        derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

'''Fast neuron IO module'''

import os
from collections import namedtuple, defaultdict
from functools import partial, update_wrapper
import numpy as np
from neurom.io.swc import SWC
from neurom.io.neurolucida import NeurolucidaASC
from neurom.core.types import NeuriteType
from neurom.core.tree import Tree, ipreorder
from neurom.core.dataformat import POINT_TYPE
from neurom.core.dataformat import COLS
from neurom.core.neuron import make_soma
from neurom.io import utils as _iout


Neuron = namedtuple('Neuron', 'soma, neurites, sections, points, data_block, name')


class Neurite(object):
    '''Class representing a neurite tree'''
    def __init__(self, root_node):
        self.root_node = root_node
        self.type = root_node.type
        self._points = None

    @property
    def points(self):
        '''Return all the points in this neurite'''
        if self._points is None:
            # add all points in a section except the first one, which is a
            # duplicate
            _pts = [v for s in ipreorder(self.root_node) for v in s.value[1:, :4]]
            # except for the very first point, which is not a duplicate
            _pts.insert(0, self.root_node.value[0][:4])
            self._points = np.array(_pts)

        return self._points

    def iter_nodes(self):
        '''unordered iteration over section nodes'''
        return ipreorder(self.root_node)


class SecDataWrapper(object):
    '''Class holding a raw data block and section information'''

    def __init__(self, data_block, fmt, sections=None):
        '''Section Data Wrapper'''
        self.data_block = data_block
        self.fmt = fmt
        self.sections = sections if sections is not None else extract_sections(data_block)

    def neurite_trunks(self):
        '''Get the section IDs of the intitial neurite sections'''
        sec = self.sections
        return [i for i, ss in enumerate(sec)
                if ss.pid > -1 and (sec[ss.pid].ntype == POINT_TYPE.SOMA and
                                    ss.ntype != POINT_TYPE.SOMA)]

    def soma_points(self):
        '''Get the soma points'''
        db = self.data_block
        return db[db[:, COLS.TYPE] == POINT_TYPE.SOMA]


_TREE_TYPES = tuple(NeuriteType)


def make_neurites(rdw, post_action=None):
    '''Build neurite trees from a raw data wrapper'''
    trunks = rdw.neurite_trunks()
    if len(trunks) == 0:
        return [], []

    start_node = min(trunks)

    # One pass over sections to build nodes
    nodes = tuple(Tree(rdw.data_block[sec.ids]) for sec in rdw.sections)

    # One pass over nodes to set the neurite type
    # and connect children to parents
    for i, node in enumerate(nodes):
        node.type = _TREE_TYPES[rdw.sections[i].ntype]
        node.section_id = i
        parent_id = rdw.sections[i].pid
        # only connect neurites
        if parent_id >= start_node:
            nodes[parent_id].add_child(node)

    neurites = tuple(Neurite(nodes[i]) for i in trunks)

    if post_action is not None:
        for n in neurites:
            post_action(n.root_node)

    return neurites, nodes


def load_neuron(filename):
    '''Build section trees from an h5 or swc file'''
    ext = os.path.splitext(filename)[1][1:]
    rdw = _READERS[ext.lower()](filename)
    neurites, sections = make_neurites(rdw, _NEURITE_ACTION[ext.lower()])
    soma = make_soma(rdw.soma_points())
    name = os.path.splitext(os.path.basename(filename))[0]
    return Neuron(soma=soma,
                  neurites=neurites,
                  sections=sections,
                  points=rdw.data_block[:, 0:4],
                  data_block=rdw,
                  name=name)


load_neurons = partial(_iout.load_neurons, neuron_loader=load_neuron)
update_wrapper(load_neurons, _iout.load_neurons)


def _merge_sections(sec_a, sec_b):
    '''Merge two sections

    Merges sec_a into sec_b and sets sec_b attributes to default
    '''
    sec_b.ids = sec_a.ids + sec_b.ids[1:]
    sec_b.ntype = sec_a.ntype
    sec_b.pid = sec_a.pid
    sec_a.ids = []
    sec_a.pid = -1


def _section_end_points(data_block):
    '''Get the section end-points '''
    # number of children per point
    n_children = defaultdict(int)
    for row in data_block:
        n_children[int(row[COLS.P])] += 1

    # end points have either no children or more than one
    return set(i for i, row in enumerate(data_block)
               if n_children[row[COLS.ID]] != 1)


def extract_sections(data_block):
    '''Make a list of sections from an SWC-style data wrapper block'''

    class Section(object):
        '''sections ((ids), type, parent_id)'''
        def __init__(self, ids=None, ntype=0, pid=-1):
            self.ids = [] if ids is None else ids
            self.ntype = ntype
            self.pid = pid

    # get SWC ID to array position map
    id_map = {-1: -1}
    for i, r in enumerate(data_block):
        id_map[int(r[COLS.ID])] = i

    # end points have either no children or more than one
    sec_end_pts = _section_end_points(data_block)

    # arfificial discontinuity section IDs
    _gap_sections = set()

    _sections = [Section()]
    curr_section = _sections[-1]
    parent_section = {-1: -1}

    for row in data_block:
        row_id = id_map[int(row[COLS.ID])]
        parent_id = id_map[int(row[COLS.P])]
        if len(curr_section.ids) == 0:
            # first in section point is parent.
            curr_section.ids.append(parent_id)
            curr_section.ntype = int(row[COLS.TYPE])
        gap = parent_id != curr_section.ids[-1]
        # If parent is not the previous point, create
        # a section end-point. Else add the point
        # to this section
        if gap:
            sec_end_pts.add(row_id)
        else:
            curr_section.ids.append(row_id)

        if row_id in sec_end_pts:
            parent_section[curr_section.ids[-1]] = len(_sections) - 1
            _sections.append(Section())
            curr_section = _sections[-1]
            # Parent-child discontinuity sectin
            if gap:
                curr_section.ids.extend((parent_id, row_id))
                curr_section.ntype = int(row[COLS.TYPE])
                _gap_sections.add(len(_sections) - 2)

    for sec in _sections:
        # get the section parent ID from the id of the first point.
        if sec.ids:
            sec.pid = parent_section[sec.ids[0]]
        # join gap sections and "disable" first half
        if sec.pid in _gap_sections:
            _merge_sections(_sections[sec.pid], sec)

    return _sections


def _remove_soma_initial_point(tree):
    '''Remove tree's initial point if soma'''
    if tree.value[0][COLS.TYPE] == POINT_TYPE.SOMA:
        tree.value = tree.value[1:]


def _load_h5(filename):
    '''Delay loading of h5py until it is needed'''
    from neurom.io.hdf5 import H5
    return H5.read(filename, remove_duplicates=False, wrapper=SecDataWrapper)


_READERS = {
    'swc': partial(SWC.read, wrapper=SecDataWrapper),
    'h5': _load_h5,
    'asc': partial(NeurolucidaASC.read, remove_duplicates=False, wrapper=SecDataWrapper)
}

_NEURITE_ACTION = {
    'swc': _remove_soma_initial_point,
    'h5': None,
    'asc': None
}
