"""
This module defines types for helping to define DNA sequence design constraints.

The key classes a :any:`Design`, :any:`Strand`, :any:`Domain` to define a DNA design, and various subclasses
of :any:`Constraint`, such as :any:`StrandConstraint` or :any:`StrandPairConstraint`,
to define constraints on the sequences assigned to each :any:`Domain` when calling
:py:meth:`search.search_for_dna_sequences`.

Also important are two other types of constraints
(not subclasses of :any:`Constraint`), which are used prior to the search to determine if it is even
legal to use a DNA sequence: subclasses of the abstract base class :any:`NumpyConstraint`,
and  :any:`SequenceConstraint`, an alias for a function taking a string as input and returning a bool.
"""

from __future__ import annotations

import os
import math
import json
from typing import List, Set, Optional, Dict, Callable, Iterable, Tuple, Union, Collection, TypeVar, Any, \
    cast, Generic, DefaultDict, FrozenSet, Iterator, Sequence, Type
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
from collections import defaultdict
import itertools
import logging
import textwrap
from multiprocessing.pool import ThreadPool
from numbers import Number
from enum import Enum, auto

import enum
import numpy as np  # noqa
from ordered_set import OrderedSet

import scadnano as sc  # type: ignore

import dsd.vienna_nupack as dv
import dsd.np as dn
from dsd.json_noindent_serializer import JSONSerializable, json_encode, NoIndent

try:
    from scadnano import Design as scDesign  # type: ignore
    from scadnano import Strand as scStrand  # type: ignore
    from scadnano import Domain as scDomain  # type: ignore
except ModuleNotFoundError:
    scDesign = Any
    scStrand = Any
    scDomain = Any

name_key = 'name'
sequence_key = 'sequence'
fixed_key = 'fixed'
label_key = 'label'
strands_key = 'strands'
domains_key = 'domains'
domain_pools_key = 'domain_pools'
domain_pools_num_sampled_key = 'domain_pools_num_sampled'
domain_names_key = 'domain_names'
starred_domain_indices_key = 'starred_domain_indices'
group_key = 'group'
domain_pool_name_key = 'pool_name'
length_key = 'length'
strand_name_in_strand_pool_key = 'strand_name'
sequences_key = 'sequences'
num_sampled_key = 'num_sampled'
max_samples_key = 'max_samples'
num_times_sequences_reset_key = 'num_times_sequences_reset'
replace_with_close_sequences_key = 'replace_with_close_sequences'
hamming_probability_key = 'hamming_probability'
num_sequences_to_generate_upper_limit_key = 'num_sequences_to_generate_upper_limit'
num_sequences_to_generate_key = 'num_sequences_to_generate'
rng_state_key = 'rng_state'

idt_key = 'idt'
idt_scale_key = 'scale'
idt_purification_key = 'purification'
idt_plate_key = 'plate'
idt_well_key = 'well'

default_idt_scale = "25nm"
default_idt_purification = "STD"

T = TypeVar('T')
KeyFunction = Callable[[T], Any]

Complex = Tuple['Strand', ...]
"""A Complex is a group of :any:`Strand`'s, in general that we expect to be bound by complementary 
:any:`Domain`'s."""

all_dna_bases: Set[str] = {'A', 'C', 'G', 'T'}
"""
Set of all DNA bases.
"""


def default_score_transfer_function(x: float) -> float:
    """
    :return: max(0.0, x**2)
    """
    return max(0.0, x ** 2)


logger = logging.Logger('dsd', level=logging.DEBUG)
"""
Global logger instance used throughout dsd.

Call ``logger.removeHandler(logger.handlers[0])`` to stop screen output (assuming that you haven't added
or removed any handlers to the dsd logger instance already; by default there is one StreamHandler, and
removing it will stop screen output).

Call ``logger.addHandler(logging.FileHandler(filename))`` to direct to a file.
"""


def _configure_logger() -> None:
    screen_handler = logging.StreamHandler()
    screen_handler.setLevel(logging.INFO)
    logger.addHandler(screen_handler)


_configure_logger()


def all_pairs(values: Iterable[T],
              with_replacement: bool = True,
              where: Callable[[Tuple[T, T]], bool] = lambda _: True) -> List[Tuple[T, T]]:
    """
    Strongly typed function to get list of all pairs from `iterable`. (for using with mypy)

    :param values:
        Iterable of values.
    :param with_replacement:
        Whether to include self pairs, i.e., pairs (a,a)
    :param where:
        Predicate indicating whether to include a specific pair.
    :return:
        List of all pairs of values from `iterable`.
    """
    return list(all_pairs_iterator(values, with_replacement=with_replacement, where=where))


def all_pairs_iterator(values: Iterable[T],
                       with_replacement: bool = True,
                       where: Callable[[Tuple[T, T]], bool] = lambda _: True) -> Iterator[Tuple[T, T]]:
    """
    Strongly typed function to get iterator of all pairs from `iterable`. (for using with mypy)

    This is WITH replacement; to specify without replacement, set `with_replacement` = False

    :param values:
        Iterable of values.
    :param with_replacement:
        Whether to include self pairs, i.e., pairs (a,a)
    :param where:
        Predicate indicating whether to include a specific pair.
    :return:
        Iterator of all pairs of values from `iterable`.
        Unlike :py:meth:`all_pairs`, which returns a list,
        the iterator returned may be iterated over only ONCE.
    """
    comb_iterator = itertools.combinations_with_replacement if with_replacement else itertools.combinations
    it = cast(Iterator[Tuple[T, T]],
              filter(where, comb_iterator(values, 2)))  # noqa
    return it


SequenceConstraint = Callable[[str], bool]
"""
Constraint that applies to a DNA sequence; the difference between this an a :any:`DomainConstraint` is
that these are applied before a sequence is assigned to a :any:`Domain`, so the constraint can only
be based on the DNA sequence, and not, for instance, on the :any:`Domain`'s :any:`DomainPool`.

Consequently :any:`SequenceConstraint`'s, like :any:`NumpyConstraint`'s, are treated differently than
subtypes of :any:`Constraint`, since a DNA sequence failing any :any:`SequenceConstraint`'s or
:any:`NumpyConstraint`'s is never allowed to be assigned into any :any:`Domain`.

The difference with :any:`NumpyConstraint` is that a :any:`NumpyConstraint` requires one to express the
constraint in a way that is efficient for the linear algebra operations of numpy. If you cannot figure out
how to do this, a :any:`SequenceConstraint` can be expressed in pure Python, but typically will be much
slower to apply than a :any:`NumpyConstraint`.
"""


# The Mypy error being ignored is a bug and is described here:
# https://github.com/python/mypy/issues/5374#issuecomment-650656381
@dataclass  # type: ignore
class NumpyConstraint(ABC):
    """
    Abstract base class for numpy constraints. These are constraints that can be efficiently encoded
    as numpy operations on 2D arrays of bytes representing DNA sequences, through the class
    :any:`np.DNASeqList` (which uses such a 2D array as the field :py:data:`np.DNASeqList.seqarr`).

    Subclasses should set the value self.name, inherited from this class.

    Pre-made subclasses of :any:`NumpyConstraint` provided in this library,
    such as :any:`RestrictBasesConstraint` or :any:`NearestNeighborEnergyConstraint`,
    are dataclasses (https://docs.python.org/3/library/dataclasses.html).
    There is no requirement that your custom subclasses be dataclasses, but since the subclasses will
    inherit the field :py:data:`NumpyConstraint.name`, you can easily make them dataclasses to get,
    for example, free ``repr`` and ``str`` implementations. See the source code for the example subclasses.
    """

    name: str = field(init=False, default='TODO: give a concrete name to this NumpyConstraint')
    """Name of this :any:`NumpyConstraint`."""

    @abstractmethod
    def remove_violating_sequences(self, seqs: dn.DNASeqList) -> dn.DNASeqList:
        """
        Subclasses should override this method.

        Since these are constraints that use numpy, generally they will access the numpy ndarray instance
        `seqs.seqarr`, operate on it, and then create a new :any:`np.DNASeqList` instance via the constructor
        :any:`np.DNASeqList` taking an numpy ndarray as input.

        See the source code of included constraints for examples, such as
        :py:meth:`NearestNeighborEnergyConstraint.remove_violating_sequences`
        or
        :py:meth:`BaseCountConstraint.remove_violating_sequences`.
        These are usually quite tricky to write, requiring one to think in terms of linear algebra
        operations. The code tends not to be easy to read. But when a constraint can be expressed
        in this way, it is typically *very* fast to apply; many millions of sequences can
        be processed in a few seconds.

        :param seqs: :any:`np.DNASeqList` object representing DNA sequences
        :return: a new :any:`np.DNASeqList` object representing the DNA sequences in `seqs` that
                 satisfy the constraint
        """
        pass


@dataclass
class RestrictBasesConstraint(NumpyConstraint):
    """
    Restricts the sequence to use only a subset of bases. This can be used to implement
    a so-called "three-letter code", for instance, in which a certain subset of :any:`Strand` uses only the
    bases A, T, C (and :any:`Strand`'s with complementary :any:`Domain` use only A, T, G), to help
    reduce secondary structure of those :any:`Strand`'s.
    See for example Supplementary Section S1.1 of
    "Scaling Up Digital Circuit Computation with DNA Strand Displacement Cascades", Qian and Winfree,
    *Science* 332:1196–1201, 2011.
    DOI: 10.1126/science.1200520,
    https://science.sciencemag.org/content/332/6034/1196,
    http://www.qianlab.caltech.edu/seesaw_digital_circuits2011_SI.pdf

    Note, however, that this is a constraint :any:`Domain`'s, not :any:`Strand`'s, so for a three-letter
    code to work, you must take care not to mixed :any:`Domain`'s on a :any:`Strand` that will use
    different alphabets.
    """  # noqa

    bases: Collection[str]
    """Bases to use. Must be a strict subset of {'A', 'C', 'G', 'T'} with at least two bases."""

    def __post_init__(self) -> None:
        self.name = 'restrict_bases'
        if not set(self.bases) < {'A', 'C', 'G', 'T'}:
            raise ValueError("bases must be a proper subset of {'A', 'C', 'G', 'T'}; "
                             f'cannot be {self.bases}')
        if len(self.bases) <= 1:
            raise ValueError('bases cannot be size 1 or smaller')

    def remove_violating_sequences(self, seqs: dn.DNASeqList) -> dn.DNASeqList:
        """Should never be called directly; it is handled specially by the library when initially
        generating sequences."""
        raise AssertionError('This should never be called directly.')


@dataclass
class NearestNeighborEnergyConstraint(NumpyConstraint):
    """
    This constraint calculates the nearest-neighbor sum of a domain with its perfect complement, using
    parameters from the 2004 Santa-Lucia and Hicks paper, and it rejects any sequences whose energy
    according to this sum is outside the range
    [:py:data:`NearestNeighborEnergyConstraint.low_energy`,
    :py:data:`NearestNeighborEnergyConstraint.high_energy`].
    """

    low_energy: float
    """Low threshold for nearest-neighbor energy."""

    high_energy: float
    """High threshold for nearest-neighbor energy."""

    temperature: float = field(default=37.0)
    """Temperature in Celsius at which to calculate nearest-neighbor energy."""

    def __post_init__(self) -> None:
        self.name = 'nearest_neighbor_energy'
        if self.low_energy > self.high_energy:
            raise ValueError(f'low_energy = {self.low_energy} must be less than '
                             f'high_energy = {self.high_energy}')

    def remove_violating_sequences(self, seqs: dn.DNASeqList) -> dn.DNASeqList:
        """Remove sequences with nearest-neighbor energies outside of an interval."""
        wcenergies = dn.calculate_wc_energies(seqs.seqarr, self.temperature)
        within_range = (self.low_energy <= wcenergies) & (wcenergies <= self.high_energy)  # type: ignore
        seqarr_pass = seqs.seqarr[within_range]  # type: ignore
        return dn.DNASeqList(seqarr=seqarr_pass)


@dataclass
class BaseCountConstraint(NumpyConstraint):
    """
    Restricts the sequence to contain a certain number of occurences of a given base.
    """

    base: str
    """Base to count."""

    high_count: Optional[int] = None
    """
    Count of :py:data:`BaseCountConstraint.base` must be at most :py:data:`BaseCountConstraint.high_count`.
    """

    low_count: Optional[int] = None
    """
    Count of :py:data:`BaseCountConstraint.base` must be at least :py:data:`BaseCountConstraint.low_count`.
    """

    def __post_init__(self) -> None:
        self.name = 'base_count'
        if self.low_count is None and self.high_count is None:
            raise ValueError('at least one of low_count or high_count must be specified')

    def remove_violating_sequences(self, seqs: dn.DNASeqList) -> dn.DNASeqList:
        """Remove sequences whose counts of a certain base are outside of an interval."""
        low_count = self.low_count if self.low_count is not None else 0
        high_count = self.high_count if self.high_count is not None else seqs.seqlen
        sumarr = np.sum(seqs.seqarr == dn.base2bits[self.base], axis=1)
        good = (low_count <= sumarr) & (sumarr <= high_count)
        seqarr_pass = seqs.seqarr[good]
        return dn.DNASeqList(seqarr=seqarr_pass)


@dataclass
class BaseEndConstraint(NumpyConstraint):
    """
    Restricts the sequence to contain only certain bases on
    (or near, if :py:data:`BaseEndConstraint.distance` > 0) each end.
    """

    bases: Collection[str]
    """Bases to require on ends."""

    distance_from_end: int = 0
    """Distance from end."""

    five_prime: bool = True
    """ Whether to apply to 5' end of sequence (left end of DNA sequence, lowest index)."""

    three_prime: bool = True
    """ Whether to apply to 3' end of sequence (right end of DNA sequence, highest index)."""

    def __post_init__(self) -> None:
        self.name = 'base_end'
        if not self.five_prime and not self.three_prime:
            raise ValueError('at least one of five_prime or three_prime must be True')
        if not (set(self.bases) < {'A', 'C', 'G', 'T'}):
            raise ValueError('bases must be a strict subset of {A,C,G,T} but is '
                             f'{self.bases}')
        if len(self.bases) == 0:
            raise ValueError('bases cannot be empty')

    def remove_violating_sequences(self, seqs: dn.DNASeqList) -> dn.DNASeqList:
        """Keeps sequences with the given bases at given distance from the 5' or 3' end."""
        all_bits = [dn.base2bits[base] for base in self.bases]

        if seqs.seqlen <= self.distance_from_end:
            raise ValueError(f'cannot specify distance from end of {self.distance_from_end} '
                             f'when sequences only have length {seqs.seqlen}')

        if self.five_prime:
            good_left = np.zeros(shape=len(seqs), dtype=np.bool)
            left = seqs.seqarr[:, self.distance_from_end]
            for bits in all_bits:
                if good_left is None:
                    good_left = (left == bits)
                else:
                    good_left |= (left == bits)

        if self.three_prime:
            good_right = np.zeros(shape=len(seqs), dtype=np.bool)
            right = seqs.seqarr[:, -1 - self.distance_from_end]
            for bits in all_bits:
                if good_right is None:
                    good_right = (right == bits)
                else:
                    good_right |= (right == bits)

        if self.five_prime and self.three_prime:
            seqarr_pass = seqs.seqarr[good_left & good_right]  # noqa
        elif self.five_prime:
            seqarr_pass = seqs.seqarr[good_left]  # noqa
        elif self.three_prime:
            seqarr_pass = seqs.seqarr[good_right]  # noqa
        else:
            raise AssertionError('unreachable')

        return dn.DNASeqList(seqarr=seqarr_pass)


@dataclass
class BaseAtPositionConstraint(NumpyConstraint):
    """
    Restricts the sequence to contain only certain base(s) on at a particular position.

    One use case is that many internal modifications (e.g., biotin or fluorophore)
    can only be placed on an T.
    """

    bases: Union[str, Collection[str]]
    """
    Base(s) to require at position :py:data:`BasePositionConstraint.position`.

    Can either be a single base, or a collection (e.g., list, tuple, set).
    If several bases are specified, the base at :py:data:`BasePositionConstraint.position`
    must be one of the bases in :py:data:`BasePositionConstraint.bases`.
    """

    position: int
    """Position of base to check."""

    def __post_init__(self) -> None:
        self.name = 'base_at_position'
        self.bases = [self.bases] if isinstance(self.bases, str) else list(self.bases)
        if not (set(self.bases) < all_dna_bases):
            raise ValueError(f'bases must be a strict subset of {all_dna_bases} but is '
                             f'{self.bases}')
        if len(self.bases) == 0:
            raise ValueError('bases cannot be empty')

    def remove_violating_sequences(self, seqs: dn.DNASeqList) -> dn.DNASeqList:
        """Remove sequences that don't have one of the given bases at the given position."""
        assert isinstance(self.bases, list)
        if not 0 <= self.position < seqs.seqlen:
            raise ValueError(f'position must be between 0 and {seqs.seqlen} but it is {self.position}')
        mid = seqs.seqarr[:, self.position]
        good = np.zeros(shape=len(seqs), dtype=np.bool)
        for base in self.bases:
            good |= (mid == dn.base2bits[base])
        seqarr_pass = seqs.seqarr[good]
        return dn.DNASeqList(seqarr=seqarr_pass)


@dataclass
class ForbiddenSubstringConstraint(NumpyConstraint):
    """
    Restricts the sequence not to contain a certain substring(s), e.g., GGGG.
    """

    substrings: Union[str, Collection[str]]
    """
    Substring(s) to forbid.

    Can either be a single substring, or a collection (e.g., list, tuple, set).
    If a collection, all substrings must have the same length.
    """

    indices: Optional[Sequence[int]] = None
    """
    Indices at which to check for each substring in :data:`ForbiddenSubstringConstraint.substrings`.
    If not specified, all appropriate indices are checked.
    """

    def __post_init__(self) -> None:
        self.name = 'forbidden_substrings'

        self.substrings = [self.substrings] if isinstance(self.substrings, str) else list(self.substrings)

        lengths = {len(substring) for substring in self.substrings}
        if len(lengths) > 1:
            raise ValueError(f'all substrings must have same length, but they have these lengths: {lengths}')

        for substring in self.substrings:
            if not (set(substring) < all_dna_bases):
                raise ValueError('must contain only letters from {A,C,G,T} but it is '
                                 f'{substring}, which has extra letters '
                                 f'{set(substring) - all_dna_bases}')
            if len(substring) == 0:
                raise ValueError('substring cannot be empty')

    def remove_violating_sequences(self, seqs: dn.DNASeqList) -> dn.DNASeqList:
        """Remove sequences that have a string in :py:data:`ForbiddenSubstringConstraint.substrings`
        as a substring."""
        assert isinstance(self.substrings, list)
        sub_len = len(self.substrings[0])
        sub_ints = [[dn.base2bits[base] for base in sub] for sub in self.substrings]
        pow_arr = [4 ** k for k in range(sub_len)]
        sub_vals = np.dot(sub_ints, pow_arr)
        toeplitz = dn.create_toeplitz(seqs.seqlen, sub_len, self.indices)
        convolution = np.dot(toeplitz, seqs.seqarr.transpose())
        pass_all = np.ones(seqs.numseqs, dtype=np.bool)
        for sub_val in sub_vals:
            pass_sub = np.all(convolution != sub_val, axis=0)
            pass_all = pass_all & pass_sub
        seqarr_pass = seqs.seqarr[pass_all]
        return dn.DNASeqList(seqarr=seqarr_pass)


@dataclass
class RunsOfBasesConstraint(NumpyConstraint):
    """
    Restricts the sequence not to contain runs of a certain length from a certain subset of bases,
    (e.g., forbidding any substring in {C,G}^3;
    no four bases can appear in a row that are either C or G)
    """

    bases: Collection[str]
    """
    Bases to forbid in runs of length :py:data:`RunsOfBasesConstraint.length`.
    """

    length: int
    """Length of run to forbid."""

    def __init__(self, bases: Union[str, Collection[str]], length: int):
        """
        :param bases: Can either be a single base, or a collection (e.g., list, tuple, set).
        :param length: length of run to forbid
        """
        self.name = 'runs_of_bases'
        self.bases = [bases] if isinstance(bases, str) else list(bases)
        self.length = length
        if not (set(self.bases) < all_dna_bases):
            raise ValueError('bases must be a strict subset of {A,C,G,T} but is '
                             f'{self.bases}')
        if len(self.bases) == 0:
            raise ValueError('bases cannot be empty')
        if self.length <= 0:
            raise ValueError(f'length must be positive, but it is {self.length}')
        if self.length == 1:
            allowed_bases = all_dna_bases - set(self.bases)
            logger.warning('You have specified a RunsOfBasesConstraint with length = 1. '
                           'Although this will work, it essentially says to forbid using any of the bases '
                           f'in {set(self.bases)}, i.e., only use bases in {allowed_bases}. '
                           f'It is more efficient to use the constraint '
                           f'RestrictBasesConstraint({allowed_bases}).')

    def remove_violating_sequences(self, seqs: dn.DNASeqList) -> dn.DNASeqList:
        """Remove sequences that have a run of given length of bases from given bases."""
        substrings = list(
            map(lambda lst: ''.join(lst), itertools.product(self.bases, repeat=self.length)))
        constraint = ForbiddenSubstringConstraint(substrings)
        return constraint.remove_violating_sequences(seqs)


@dataclass
class DomainPool:
    """
    Represents a group of related :any:`Domain`'s that share common properties in their sequence design,
    such as length of DNA sequence, or bounds on nearest-neighbor duplex energy.

    Also serves as a "source" of DNA sequences for :any:`Domain`'s in this :any:`DomainPool`.
    By calling :py:meth:`DomainPool.generate_sequence` repeatedly, we can produce DNA sequences satisfying
    the constraints defining this :any:`DomainPool`.
    """

    name: str
    """Name of this :any:`DomainPool`. Must be unique."""

    length: int
    """Length of DNA sequences generated by this :any:`DomainPool`."""

    numpy_constraints: List[NumpyConstraint] = field(
        compare=False, hash=False, default_factory=list, repr=False)
    """
    :any:`NumpyConstraint`'s shared by all :any:`Domain`'s in this :any:`DomainPool`.
    This is used to choose potential sequences to assign to the :any:`Domain`'s in this :any:`DomainPool`
    in the method :py:meth:`DomainPool.generate`.

    The difference with :py:data:`DomainPool.sequence_constraints` is that these constraints can be applied
    efficiently to many sequences at once, represented as a numpy 2D array of bytes (via the class
    :any:`np.DNASeqList`), so they are done in large batches in advance.
    In contrast, the constraints in :py:data:`DomainPool.sequence_constraints` are done on Python strings
    representing DNA sequences, and they are called one at a time when a new sequence is requested in
    :py:meth:`DomainPool.generate_sequence`.

    Optional; default is empty.
    """

    sequence_constraints: List[SequenceConstraint] = field(
        compare=False, hash=False, default_factory=list, repr=False)
    """
    :any:`SequenceConstraint`'s shared by all :any:`Domain`'s in this :any:`DomainPool`.
    This is used to choose potential sequences to assign to the :any:`Domain`'s in this :any:`DomainPool`
    in the method :py:meth:`DomainPool.generate`.

    See :py:data:`DomainPool.numpy_constraints` for an explanation of the difference between them.

    See :py:data:`DomainPool.domain_constraints` for an explanation of the difference between them.

    Optional; default is empty.
    """

    sequences: dn.DNASeqList = field(compare=False, hash=False,
                                     default_factory=lambda: dn.DNASeqList(length=0), repr=False)
    # list of available sequences.
    # We sample with replacement (uniform if :data:`DomainPool.replace_with_close_sequences` = False,
    # according to :data:`DomainPool.hamming_probability` otherwise)
    # This is represented as a DNASeqList for efficiency when calculating Hamming distances
    # when the option `replace_with_close_sequences` is True.

    num_sampled: int = 0

    max_samples: Optional[int] = None

    num_times_sequences_reset: int = 0

    replace_with_close_sequences: bool = False
    """
    If True, instead of picking a sequence uniformly at random from all those satisfying the constraints
    when returning a sequence from :meth:`DomainPool.generate_sequence`,
    one is picked "close" in Hamming distance to the previous sequence of the :any:`Domain`.
    The field :data:`DomainPool.hamming_probability` is used to pick a distance at random, after which
    a sequence that distance from the previous sequence is selected to return from .
    """

    hamming_probability: Dict[int, float] = field(default_factory=dict)
    """
    Dictionary that specifies probability of taking a new sequence from the pool that is some integer 
    number of bases different from the previous sequence (Hamming distance). 
    """

    num_sequences_to_generate_upper_limit: int = 4 ** 12
    """
    Maximum number of sequences to generate at once. If :py:data:`DomainPool.num_sequences_to_generate` 
    exceeds this number, the user is asked to re-adjust constraint strictness. The number may be changed to 
    satisfy individual device capabilities.  
    """

    num_sequences_to_generate: int = 10 ** 5
    """
    Number of sequences to be generated at once. Will be increased if no sequences satisfying constraints 
    are found after generating the current specified amount of sequences. 
    """

    def __post_init__(self) -> None:
        if len(self.hamming_probability) == 0:  # sets default probability distribution if the user does not
            for i in range(self.length):
                # exponentially decreasing probability of making i+1 (since i starts at 0) base changes
                self.hamming_probability[i + 1] = 1 / 2 ** (i + 1)
            self.hamming_probability[self.length] *= 2

        idx = 0
        for numpy_constraint in self.numpy_constraints:
            if not isinstance(numpy_constraint, NumpyConstraint):
                raise ValueError('each element of numpy_constraints must be an instance of NumpyConstraint, '
                                 f'but the element at index {idx} is of type {type(numpy_constraint)}')
            idx += 1

        idx = 0
        for seq_constraint in self.sequence_constraints:
            # SequenceConstraint is an alias for Callable[[str], float],
            # which is not checkable using isinstance
            # https://stackoverflow.com/questions/624926/how-do-i-detect-whether-a-python-variable-is-a-function
            if not callable(seq_constraint):
                raise ValueError('each element of numpy_constraints must be an instance of '
                                 'SequenceConstraint (i.e., be a function that takes a single string '
                                 'and returns a bool), '
                                 f'but the element at index {idx} is of type {type(seq_constraint)}')
            idx += 1

    def to_json(self, include_sequences: bool) -> str:
        json_map = self.to_json_serializable(include_sequences)
        json_str = json.dumps(json_map, indent=2)
        return json_str

    def to_json_serializable(self, include_sequences: bool) -> Dict[str, Any]:
        dct = {
            name_key: self.name,
            length_key: self.length,
            num_sampled_key: self.num_sampled,
            max_samples_key: self.max_samples,
            num_times_sequences_reset_key: self.num_times_sequences_reset,
            replace_with_close_sequences_key: self.replace_with_close_sequences,
            hamming_probability_key: self.hamming_probability,
            num_sequences_to_generate_upper_limit_key: self.num_sequences_to_generate_upper_limit,
            num_sequences_to_generate_key: self.num_sequences_to_generate
        }
        if include_sequences:
            dct[sequences_key] = self.sequences.to_list()
        return dct

    @staticmethod
    def from_json_serializable(json_map: Dict[str, Any]) -> DomainPool:
        name = json_map[name_key]
        length = json_map[length_key]
        num_sampled = json_map[num_sampled_key]
        max_samples = json_map[max_samples_key]
        num_times_sequences_reset = json_map[num_times_sequences_reset_key]
        replace_with_close_sequences = json_map[replace_with_close_sequences_key]
        hamming_probability_str_keys = json_map[hamming_probability_key]
        hamming_probability = {int(key): val for key, val in hamming_probability_str_keys.items()}
        num_sequences_to_generate_upper_limit = json_map[num_sequences_to_generate_upper_limit_key]
        num_sequences_to_generate = json_map[num_sequences_to_generate_key]
        sequences_list = json_map[sequences_key]
        sequences = dn.DNASeqList(seqs=sequences_list)
        return DomainPool(name=name, length=length, sequences=sequences,
                          num_sampled=num_sampled, max_samples=max_samples,
                          num_times_sequences_reset=num_times_sequences_reset,
                          replace_with_close_sequences=replace_with_close_sequences,
                          hamming_probability=hamming_probability,
                          num_sequences_to_generate_upper_limit=num_sequences_to_generate_upper_limit,
                          num_sequences_to_generate=num_sequences_to_generate
                          )

    def _reset_precomputed_sequences(self, rng: np.random.Generator) -> None:
        # precomputes a new list of sequences satisfying numpy constraints and sequence constraints
        self.sequences, use_random_subset = self._generate_sequences_satisfying_numpy_constraints(rng)
        self._filter_sequence_constraints()
        self.num_sampled = 0
        self.num_times_sequences_reset += 1

        if use_random_subset:
            # sampling 2 * self.sequences.numseqs uniformly at random will leave 1/e^2 fraction unsampled
            # in expectation: https://www.cs.purdue.edu/homes/hmaji/teaching/Spring%202017/lectures/03.pdf
            self.max_samples = 2 * self.sequences.numseqs
        else:
            # if we didn't pick a random subset, we have all the sequences of this length satisfying
            # the constraints, so never reset and just keep sampling them with replacement forever
            self.max_samples = None

    def _filter_sequence_constraints(self) -> None:
        if len(self.sequence_constraints) == 0:
            return
        idxs_to_keep = []
        for idx in range(self.sequences.numseqs):
            seq = self.sequences.get_seq_str(idx)
            if self.satisfies_sequence_constraints(seq):
                idxs_to_keep.append(idx)
        self.sequences.keep_seqs_at_indices(idxs_to_keep)

    def __hash__(self) -> int:
        return hash((self.name, self.length))

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, DomainPool):
            return False
        return self.name == other.name and self.length == other.length

    def satisfies_sequence_constraints(self, sequence: str) -> bool:
        """
        :param sequence: DNA sequence to check
        :return: whether `sequence` satisfies all constraints in :py:data:`DomainPool.sequence_constraints`
        """
        return all(constraint(sequence) for constraint in self.sequence_constraints)

    def find_hamming_distances(self, previous_sequence: str) -> Dict[int, dn.DNASeqList]:
        """
        Makes a dictionary mapping a Hamming distance to a :any:`DNASeqList`, corresponding to how many bases
        different the sequence is from `previous_sequence` (i.e., the Hamming distance).

        :param previous_sequence:
            DNA sequence to be find Hamming distances from
        :return:
            dictionary mapping Hamming distances to sequences (represented as :any:`DNASeqList`)
            that are that Hamming distance from `previous_sequence`
        """
        return self.sequences.hamming_map(previous_sequence)

    def sequences_at_hamming_distance(self, previous_sequence: str,
                                      distance: int) -> dn.DNASeqList:
        """
        Returns a :any:`DNASeqList`, which are `distance` bases
        different from `previous_sequence` (i.e., the Hamming distance).

        :param previous_sequence:
            DNA sequence to be find Hamming distances from
        :param distance:
            target Hamming distance
        :return:
            :any:`DNASeqList` of sequences that are that Hamming distance from `previous_sequence`
        """
        return self.sequences.sequences_at_hamming_distance(previous_sequence, distance)

    def generate_sequence(self, rng: np.random.Generator, previous_sequence: Optional[str] = None) -> str:
        """
        Returns a DNA sequence of given length satisfying :py:data:`DomainPool.numpy_constraints` and
        :py:data:`DomainPool.sequence_constraints`

        **Note:** By default, there is no check that the sequence returned is unequal to one already
        assigned somewhere in the design, since both :py:data:`DomainPool.numpy_constraints` and
        :py:data:`DomainPool.sequence_constraints` do not have access to the whole :any:`Design`.
        But the :any:`DomainPairConstraint` returned by
        :py:meth:`domains_not_substrings_of_each_other_domain_pair_constraint`
        can be used to specify this :any:`Design`-wide constraint.

        :param rng:
            numpy random number generator to use. To use a default, pass :py:data:`np.default_rng`.
        :param previous_sequence:
            previously generated sequence to be replaced by a new sequence; None if no previous
            sequence exists. Used in :py:meth:`DomainPool.find_steps_distance_sequences`
            to choose a new sequence "close" to itself in Hamming distance. The number of
            differences between previous_sequence and its neighbors is determined by randomly
            picking a Hamming distance from :py:data:`DomainPool.hamming_probability` with
            weighted probabilities of choosing each distance.
        :return:
            DNA sequence of given length satisfying :py:data:`DomainPool.numpy_constraints` and
            :py:data:`DomainPool.sequence_constraints`
        """
        if not self.replace_with_close_sequences or previous_sequence is None:
            # takes a completely random sequence from domain pool
            sequence = self._get_next_sequence_satisfying_numpy_and_sequence_constraints(rng)
        else:
            # import time
            # before = time.perf_counter_ns()
            sequence = self._sample_hamming_distance_from_sequence(previous_sequence, rng, True)
            # after = time.perf_counter_ns()
            # print(f'time spent sampling Hamming neighbors: {(after - before) / 1e6:.1f} ms')

        self.num_sampled += 1

        return sequence

    def _sample_hamming_distance_from_sequence(self, previous_sequence: str, rng: np.random.Generator,
                                               pick_distance_first: bool) -> str:
        # all possible distances from 1 to len(previous_sequence) are calculated.
        # if pick_distance_first is true, we save some time by not gathering sequences at all distances
        # into a dict. Instead we first sample a distance and then look up only those sequences at that
        # distance to sample. The tradeoff is that if there are no sequences at that distance, we have
        # to re-sample. Given that we now keep every sequence in the DomainPool available (i.e.,
        # sampling with replacement) until we regenerate them all, hopefully this is fairly efficient.

        if self.sequences.numseqs == 0 or (
                self.max_samples is not None and self.num_sampled >= self.max_samples):
            if self.sequences.numseqs > 0:
                logger.info('Twice as many pool sequences have been sampled with replacement from a '
                            'randomly chosen subset. Regenerating fresh sequences.')
            self._reset_precomputed_sequences(rng)
        # takes neighbor to previous sequence; difference in bases randomly chosen
        hamming_probabilities = np.array(list(self.hamming_probability.values()))

        if pick_distance_first:
            # pick a distance at random, then re-pick if no sequences are at that distance
            sequence = None
            previous_sequence_1d_array = dn.seq2arr(previous_sequence)
            available_distances_list = list(range(1, len(previous_sequence) + 1))

            # For efficiency we inline the logic of self.sequences_at_hamming_distance()
            # The next line is the most expensive part of the calculation, so we only do it once,
            # rather than calling self.sequences_at_hamming_distance() repeatedly, which would recalculate
            # the next line for each sampled distance even though it doesn't change.
            computed_distances = np.sum(
                np.bitwise_xor(self.sequences.seqarr, previous_sequence_1d_array) != 0, axis=1)

            while sequence is None:
                available_distances_arr = np.array(available_distances_list)
                existing_hamming_probabilities = hamming_probabilities[available_distances_arr - 1]
                prob_sum = existing_hamming_probabilities.sum()
                existing_hamming_probabilities /= prob_sum
                sampled_distance = rng.choice(available_distances_arr, p=existing_hamming_probabilities)

                # sequences = self.sequences_at_hamming_distance(previous_sequence, distance)
                indices_at_distance = computed_distances == sampled_distance
                arr = self.sequences.seqarr[indices_at_distance]
                sequences = dn.DNASeqList(seqarr=arr)

                if sequences.numseqs == 0:
                    logger.debug(f'found no sequences Hamming distance {sampled_distance} '
                                 f'from {previous_sequence}; sampling a new distance')
                    available_distances_list.remove(sampled_distance)
                else:
                    sequence = rng.choice(sequences)

        else:
            # first determine all sequences at all distances, then pick distance at random from list of
            # distances that have at least one sequence at that distance (takes more time usually)
            neighbors = self.find_hamming_distances(previous_sequence)

            # Some distances may not exist, so scale the probabilities of the remaining so they sum to one
            available_distances_arr = np.array(list(neighbors.keys()))
            existing_hamming_probabilities = hamming_probabilities[available_distances_arr - 1]
            prob_sum = existing_hamming_probabilities.sum()
            existing_hamming_probabilities /= prob_sum

            # randomly chooses a Hamming distance to change previous_sequence by
            sampled_distance = rng.choice(available_distances_arr, p=existing_hamming_probabilities)
            assert len(neighbors[sampled_distance]) > 0
            sequence = rng.choice(neighbors[sampled_distance])

        return sequence

    def _get_next_sequence_satisfying_numpy_and_sequence_constraints(self, rng: np.random.Generator) -> str:
        if self.sequences.numseqs == 0 or self.num_sampled >= len(self.sequences):
            self._reset_precomputed_sequences(rng)
        idx = int(rng.integers(self.sequences.numseqs))
        sequence = self.sequences[idx]
        self.num_sampled += 1
        return sequence

    def _generate_sequences_satisfying_numpy_constraints(self, rng: np.random.Generator) \
            -> Tuple[dn.DNASeqList, bool]:
        bases = self._bases_to_use()
        length = self.length
        # For lengths at most _length_threshold_numpy, we generate all DNA sequences in advance.
        # Above this value, a random subset of DNA sequences will be generated.
        _length_threshold_numpy = math.floor(math.log(self.num_sequences_to_generate, 4))
        # _length_threshold_numpy = 10
        use_random_subset = length > _length_threshold_numpy
        if not use_random_subset:
            seqs = dn.DNASeqList(length=length, alphabet=bases, shuffle=True, rng=rng)
            num_starting_seqs = seqs.numseqs
        else:
            num_starting_seqs = self.num_sequences_to_generate
            seqs = dn.DNASeqList(length=length, alphabet=bases, shuffle=True,
                                 num_random_seqs=num_starting_seqs, rng=rng)
        seqs_satisfying_numpy_constraints = self._filter_numpy_constraints(seqs)
        while seqs_satisfying_numpy_constraints.numseqs == 0 and \
                self.num_sequences_to_generate < self.num_sequences_to_generate_upper_limit:
            # 4 ** 13 sequences or more takes over a minute to generate and an excessive amount of memory
            print(f'No valid sequences found in {self.num_sequences_to_generate} sequences examined. '
                  f'Increasing number of sequences generated by a factor of 10.')
            self.num_sequences_to_generate *= 10
            num_starting_seqs = self.num_sequences_to_generate
            seqs = dn.DNASeqList(length=length, alphabet=bases, shuffle=True,
                                 num_random_seqs=num_starting_seqs, rng=rng)
            seqs_satisfying_numpy_constraints = self._filter_numpy_constraints(seqs)
        else:
            if self.num_sequences_to_generate >= self.num_sequences_to_generate_upper_limit:
                raise ValueError("Too many sequences need to be generated to find a satisfactory sequence. "
                                 "Please adjust constraints. ")
        num_decimals = len(str(self.num_sequences_to_generate))

        logger.info(f'generated {num_starting_seqs:{num_decimals}} sequences '
                    f'of length {length:2}, '
                    f'of which {len(seqs_satisfying_numpy_constraints):{num_decimals}} '
                    f'passed the numpy sequence constraints'
                    f'{" (generated at random)" if use_random_subset else ""}')
        return seqs_satisfying_numpy_constraints, use_random_subset

    def _bases_to_use(self) -> Collection[str]:
        # checks explicitly for NumpyRestrictBasesConstraint
        for constraint in self.numpy_constraints:
            if isinstance(constraint, RestrictBasesConstraint):
                return constraint.bases
        return 'A', 'C', 'G', 'T'

    def _filter_numpy_constraints(self, seqs: dn.DNASeqList) -> dn.DNASeqList:
        # filter sequence not passing numpy constraints, but skip NumpyRestrictBasesConstraint since
        # that is more efficiently handled by the DNASeqList constructor to generate the sequences
        # in the first place
        for constraint in self.numpy_constraints:
            if isinstance(constraint, RestrictBasesConstraint):
                continue
            seqs = constraint.remove_violating_sequences(seqs)
        return seqs


def add_quotes(string: str) -> str:
    # adds quotes around a string
    return f'"{string}"'


def mandatory_field(ret_type: Type, json_map: Dict, main_key: str, *legacy_keys: str) -> Any:
    # should be called from function whose return type is the type being constructed from JSON, e.g.,
    # Design or Strand, given by ret_type. This helps give a useful error message
    for key in (main_key,) + legacy_keys:
        if key in json_map:
            return json_map[key]
    ret_type_name = ret_type.__name__
    msg_about_keys = f'the key "{main_key}"'
    if len(legacy_keys) > 0:
        msg_about_keys += f" (or any of the following legacy keys: {', '.join(map(add_quotes, legacy_keys))})"
    msg = f'I was looking for {msg_about_keys} in the JSON encoding of a {ret_type_name}, ' \
          f'but I did not find it.' \
          f'\n\nThis occurred when reading this JSON object:\n{json_map}'
    raise ValueError(msg)


DomainLabel = TypeVar('DomainLabel')


@dataclass
class Domain(JSONSerializable, Generic[DomainLabel]):
    """
    Represents a contiguous substring of the DNA sequence of a :any:`Strand`, which is intended
    to be either single-stranded, or to bind fully to the Watson-Crick complement of the :any:`Domain`.

    If two domains are complementary, they are represented by the same :any:`Domain` object.
    They are distinguished only by whether the :any:`Strand` object containing them has the
    :any:`Domain` in its set :py:data:`Strand.starred_domains` or not.

    A :any:`Domain` uses only its name to compute hash and equality checks, not its sequence.
    This allows a :any:`Domain` to be used in sets and dicts while modifying the sequence assigned to it,
    and also modifying the pool (letting the pool be assigned after it is created).
    """

    _name: str
    """
    Name of the :any:`Domain`.
    This is the "unstarred" version of the name, and it cannot end in `*`.
    """

    _starred_name: str

    _pool: Optional[DomainPool] = field(init=False, default=None, compare=False, hash=False)
    """
    Each :any:`Domain` in the same :any:`DomainPool` as this one share a set of properties, such as
    length and individual :any:`DomainConstraint`'s.
    """

    # TODO: `set_sequence_recursive_up`

    #        - if parent is not none, make recursive call to set_sequence_recursive_up
    # TODO: `set_sequence_recursive_down`
    #        - iterate over children, call set_sequence
    _sequence: Optional[str] = field(init=False, repr=False, default=None, compare=False, hash=False)
    """
    DNA sequence assigned to this :any:`Domain`. This is assumed to be the sequence of the unstarred
    variant; the starred variant has the Watson-Crick complement,
    accessible via :py:data:`Domain.starred_sequence`.
    """

    fixed: bool = False
    """
    Whether this :any:`Domain`'s DNA sequence is fixed, i.e., cannot be changed by the
    search algorithm :py:meth:`search.search_for_dna_sequences`.

    Note: If a domain is fixed then all of its subdomains must also be fixed.
    """

    label: Optional[DomainLabel] = None
    """
    Optional generic "label" object to associate to this :any:`Domain`.

    Useful for associating extra information with the :any:`Domain` that will be serialized, for example,
    for DNA sequence design. It must be an object (e.g., a dict or primitive type such as str or int)
    that is naturally JSON serializable. (Calling
    `json.dumps <https://docs.python.org/3/library/json.html#json.dumps>`_
    on the object should succeed without having to specify a custom encoder.)
    """

    dependent: bool = False
    """
    Whether this :any:`Domain`'s DNA sequence is dependent on others. Usually this is not the case.
    However, domains can be subdivided hierarchically into a tree of domains. In this case exactly
    one domain along every path from the root to any leaf must be independent, and the rest dependent:
    the dependent domains will have their sequences calculated from the indepenedent ones.
    
    A possible use case is that one strand represents a subsequence of M13 of length 300,
    of which there are 7249 possible DNA sequences to assign based on the different
    rotations of M13. If this strand is bound to several other strands, it will have
    several domains, but they cannot be set independently of each other.
    This can be done by creating a strand with a single long domain, which is subdivided into many dependent 
    child domains.
    Only the entire strand, the root domain, can be assigned at once, changing every domain at once,
    so the domains are dependent on the root domain's assigned sequence.
    """

    _subdomains: List[Domain] = field(init=False, default_factory=list)
    """List of smaller subdomains whose concatenation is this domain. If empty, then there are no subdomains.
    """

    parent: Optional[Domain] = field(init=False, default=None)
    """Domain of which this is a subdomain. Note, this is not set manually, this is set by the library based 
    on the :py:data:`Domain.subdomains` of other domains in the same tree.
    """

    def __init__(self, name: str, pool: Optional[DomainPool] = None, sequence: Optional[str] = None,
                 fixed: bool = False, label: Optional[DomainLabel] = None, dependent: bool = False,
                 subdomains: Optional[List["Domain"]] = None) -> None:
        if subdomains is None:
            subdomains = []
        self._name = name
        self._starred_name = name + '*'
        self._pool = pool
        self._sequence = sequence
        self._starred_sequence = None if sequence is None else dv.wc(sequence)
        self.fixed = fixed
        self.label = label
        self.dependent = dependent
        self._subdomains = subdomains

        self.__post_init__()

    def __post_init__(self) -> None:
        if self.name.endswith('*'):
            raise ValueError('Domain name cannot end with *\n'
                             f'domain name = {self.name}')

        if self.fixed:
            for sd in self._subdomains:
                if not sd.fixed:
                    raise ValueError(f'Domain is fixed, but subdomain {sd} is not fixed')
        else:
            contains_no_non_fixed_subdomains = True
            for sd in self._subdomains:
                if not sd.fixed:
                    contains_no_non_fixed_subdomains = False
                    break
            if len(self._subdomains) > 0 and contains_no_non_fixed_subdomains:
                raise ValueError(f'Domain is not fixed, but all subdomains are fixed')

        # Set parent field for all subdomains.
        for subdomain in self._subdomains:
            subdomain.parent = self

    def to_json_serializable(self, suppress_indent: bool = True) -> Union[NoIndent, Dict[str, Any]]:
        """
        :return:
            Dictionary ``d`` representing this :any:`Domain` that is "naturally" JSON serializable,
            by calling ``json.dumps(d)``.
        """
        dct: Dict[str, Any] = {name_key: self.name}
        if self._pool is not None:
            dct[domain_pool_name_key] = self._pool.name
        if self.has_sequence():
            dct[sequence_key] = self._sequence
            if self.fixed:
                dct[fixed_key] = True
        if self.label is not None:
            dct[label_key] = self.label
        return NoIndent(dct) if suppress_indent else dct

    @staticmethod
    def from_json_serializable(json_map: Dict[str, Any],
                               pool_with_name: Optional[Dict[str, DomainPool]],
                               label_decoder: Callable[[Any], DomainLabel] = lambda label: label) \
            -> Domain[DomainLabel]:
        """
        :param json_map:
            JSON serializable object encoding this :any:`Domain`, as returned by
            :py:meth:`Domain.to_json_serializable`.
        :param pool_with_name:
            dict mapping name to :any:`DomainPool` with that name; required to rehydrate :any:`Domain`'s.
            If None, then a DomainPool with no constraints is created with the name and domain length
            found in the JSON.
        :param label_decoder:
            Function transforming object deserialized from JSON  (e.g, dict, list, string) into an object
            of type DomainLabel.
        :return:
            :any:`Domain` represented by dict `json_map`, assuming it was created by
            :py:meth:`Domain.to_json_serializable`.
        """
        name: str = mandatory_field(Domain, json_map, name_key)
        sequence: Optional[str] = json_map.get(sequence_key)
        fixed: bool = json_map.get(fixed_key, False)

        label_json: Any = json_map.get(label_key)
        label = label_decoder(label_json)

        pool: Optional[DomainPool]
        pool_name: Optional[str] = json_map.get(domain_pool_name_key)
        if pool_name is not None:
            if pool_with_name is not None:
                pool = pool_with_name[pool_name] if pool_with_name is not None else None
            else:
                raise AssertionError()
        else:
            pool = None

        domain: Domain[DomainLabel] = Domain(
            name=name, sequence=sequence, fixed=fixed, pool=pool, label=label)
        return domain

    def __hash__(self) -> int:
        return hash(self._name)

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Domain):
            return False
        return self._name == other._name

    def __repr__(self) -> str:
        return self._name

    @property
    def name(self) -> str:
        """
        :return: :any:`DomainPool` of this :any:`Domain`
        """
        return self._name

    @name.setter
    def name(self, new_name: str) -> None:
        """
        :param new_name: new name to set
        """
        self._name = new_name
        self._starred_name = new_name + '*'

    @property
    def pool(self) -> DomainPool:
        """
        :return: :any:`DomainPool` of this :any:`Domain`
        """
        if self._pool is None:
            raise ValueError(f'pool has not been set for Domain {self.name}')
        return self._pool

    @pool.setter
    def pool(self, new_pool: DomainPool) -> None:
        """
        :param new_pool: new :any:`DomainPool` to set
        :raises ValueError: if :py:data:`Domain.pool_` is not None and is not same object as `new_pool`
        """
        if self._pool is not None and new_pool is not self._pool:
            raise ValueError(f'Assigning pool {new_pool} to domain '
                             f'{self} but {self} already has domain '
                             f'pool {self._pool}')
        self._pool = new_pool

    @property
    def subdomains(self) -> List["Domain"]:
        return self._subdomains

    @subdomains.setter
    def subdomains(self, new_subdomains: List["Domain"]) -> None:
        self._subdomains = new_subdomains
        for s in new_subdomains:
            s.parent = self

    def has_length(self) -> bool:
        """
        :return:
            True if this :any:`Domain` has a length, which means either a sequence has been assigned
            to it, or it has a :any:`DomainPool`.
        """
        return self._sequence is not None or self._pool is not None

    @property
    def length(self) -> int:
        """
        :return: Length of this domain (delegates to pool)
        :raises ValueError: if no :any:`DomainPool` has been set for this :any:`Domain`
        """
        if self.fixed and self._sequence is not None:
            return len(self._sequence)
        if self._pool is None:
            raise ValueError('No DomainPool has been set for this Domain, so it has no length yet.\n'
                             'Assign a DomainPool (which has a length field) to give this Domain a length.')
        return self._pool.length

    def sequence(self) -> str:
        """
        :return: DNA sequence of this domain (unstarred version)
        :raises ValueError: If no sequence has been assigned.
        """
        if self._sequence is None or '?' in self._sequence:
            raise ValueError(f'sequence has not been set for Domain {self.name}\n'
                             f'sequence: {self._sequence}')
        return self._sequence

    def set_sequence(self, new_sequence: str) -> None:
        """
        :param new_sequence: new DNA sequence to set
        """
        if self.fixed:
            raise ValueError('cannot assign a new sequence to this Domain; its sequence is fixed as '
                             f'{self._sequence}')
        if self.has_length() and len(new_sequence) != self.length:
            raise ValueError(f'new_sequence={new_sequence} is not the correct length; '
                             f'it is length {len(new_sequence)}, but this domain is length {self.length}')
        # Check that total length of subdomains (if used) adds up domain length.
        if len(self._subdomains) != 0:
            sd_total_length = 0
            for sd in self._subdomains:
                sd_total_length += sd.length
            if sd_total_length != self.length:
                raise ValueError(
                    f'Domain {self} is length {self.length} but subdomains {self._subdomains} has total '
                    f'length of {sd_total_length}')
        self._sequence = new_sequence
        self._starred_sequence = dv.wc(new_sequence)
        self._set_subdomain_sequences(new_sequence)
        self._set_parent_sequence(new_sequence)

    def _set_subdomain_sequences(self, new_sequence: str) -> None:
        """Sets sequence for all subdomains.

        :param new_sequence: Sequence assigned to this domain.
        :type new_sequence: str
        """
        sequence_idx = 0
        for sd in self._subdomains:
            sd_len = sd.length
            sd_sequence = new_sequence[sequence_idx: sequence_idx + sd_len]
            sd._sequence = sd_sequence
            sd._set_subdomain_sequences(sd_sequence)
            sequence_idx += sd_len

    def _set_parent_sequence(self, new_sequence: str) -> None:
        """Set parent sequence and propagate upwards

        :param new_sequence: new sequence
        :type new_sequence: str
        """
        parent = self.parent
        if parent is not None:
            if parent._sequence is None:
                parent._sequence = '?' * parent.length
            # Add up lengths of subdomains, add new_sequence
            idx = 0
            assert self in parent._subdomains
            sd: Optional[Domain] = None
            for sd in parent._subdomains:
                if sd == self:
                    break
                else:
                    idx += sd.length
            assert sd is not None
            old_sequence = parent._sequence
            parent._sequence = old_sequence[:idx] + new_sequence + old_sequence[idx + sd.length:]
            parent._set_parent_sequence(parent._sequence)

    def set_fixed_sequence(self, fixed_sequence: str) -> None:
        """
        Set DNA sequence and fix it so it is not changed by the dsd sequence designer.

        Since it is being fixed, there is no Domain pool, so we don't check the pool or whether it has
        a length. We also bypass the check that it is not fixed.

        :param fixed_sequence: new fixed DNA sequence to set
        """
        self._sequence = fixed_sequence
        self._starred_sequence = dv.wc(fixed_sequence)
        self._set_subdomain_sequences(fixed_sequence)
        self._set_parent_sequence(fixed_sequence)
        self.fixed = True

    @property
    def starred_name(self) -> str:
        """
        :return: The value :py:data:`Domain.name` with `*` appended to it.
        """
        return self._starred_name

    @property
    def starred_sequence(self) -> str:
        """
        :return: Watson-Crick complement of DNA sequence assigned to this :any:`Domain`.
        """
        if self._sequence is None:
            raise ValueError('no DNA sequence has been assigned to this Domain')
        # return dv.wc(self.sequence)
        return self._starred_sequence

    def get_name(self, starred: bool) -> str:
        """
        :param starred: whether to return the starred or unstarred version of the name
        :return: The value :py:data:`Domain.name` or :py:data:`Domain.starred_name`, depending on
                 the value of parameter `starred`.
        """
        return self._starred_name if starred else self._name

    def concrete_sequence(self, starred: bool) -> str:
        """
        :param starred: whether to return the starred or unstarred version of the sequence
        :return: The value :py:data:`Domain.sequence` or :py:data:`Domain.starred_sequence`, depending on
                 the value of parameter `starred`.
        :raises ValueError: if this :any:`Domain` does not have a sequence assigned
        """
        if self._sequence is None:
            raise ValueError('no DNA sequence has been assigned to this Domain')
        if self._starred_sequence is None:
            raise AssertionError('_starred_sequence should be set to non-None if _sequence is not None. '
                                 'Something went wrong in the logic of dsd.')
        return self._starred_sequence if starred else self._sequence

    def has_sequence(self) -> bool:
        """
        :return: Whether a complete DNA sequence has been assigned to this :any:`Domain`.
                 If this domain has subdomains, False if any subdomain has not been assigned
                 a sequence.
        """
        return self._sequence is not None and '?' not in self._sequence

    @staticmethod
    def complementary_domain_name(domain_name: str) -> str:
        """
        Returns the name of the domain complementary to `domain_name`
        :param domain_name: name of domain
        """
        return domain_name[:-1] if domain_name[-1] == '*' else domain_name + '*'

    def _is_independent(self) -> bool:
        """Return true if self is independent (not dependent or fixed).

        :return: [description]
        :rtype: bool
        """
        return not self.dependent or self.fixed

    def _contains_any_independent_subdomain_recursively(self) -> bool:
        """Returns true if the subdomain graph rooted at this domain contains
        at least one independent subdomain.

        :rtype: bool
        """
        if self._is_independent():
            return True

        for sd in self._subdomains:
            if sd._contains_any_independent_subdomain_recursively():
                return True

        return False

    def _check_subdomain_graph_is_uniquely_assignable(self) -> None:
        """Checks that the subdomain graph that this domain is part of is
        uniquely assignable. Meaning that all paths from the root to the
        leaf of the subdomain graph contains exaclty one independent subdomain.
        """
        if self.parent is None:
            self._check_exactly_one_independent_subdomain_all_paths()
        else:
            self.parent._check_subdomain_graph_is_uniquely_assignable()

    def _check_exactly_one_independent_subdomain_all_paths(self) -> None:
        """Checks if all paths in the subdomains graph from the self to
        a leaf subdomain contains exactly one independent (dependent = False or
        fixed = True) subdomain (could be this one).

        :raises ValueError: if condition is not satisfied
        """
        self_independent = not self.dependent or self.fixed

        if self_independent:
            # Since this domain is independent, check that there are no more independent subdomains
            # in any children recursively
            for sd in self._subdomains:
                if sd._contains_any_independent_subdomain_recursively():
                    # Too many independent subdomains in this path
                    raise ValueError(f"Domain {self} is independent, but subdomain {sd} already contains an "
                                     f"independent subdomain in its subdomain graph")
        else:
            if len(self._subdomains) == 0:
                raise ValueError(f"Domain {self} is dependent and does not contain any subdomains.")
            # Since this domain is dependent, check that each subdomain has
            # exactly one independent subdomain in all paths.
            for sd in self._subdomains:
                try:
                    sd._check_exactly_one_independent_subdomain_all_paths()
                except ValueError as e:
                    raise ValueError(
                        f"Domain {self} is dependent and could not find exactly one independent subdomain "
                        f"in subdomain graph rooted at subdomain {sd}. The following error was found: {e}")

    def _check_acyclic_subdomain_graph(self, seen_domains: Optional[Set["Domain"]] = None) -> None:
        """Check to see if domain's subdomain graph contains a cycle.

        :param seen_domains: All the domains seen so far (used by implementation)
        :type seen_domains: Optional[Set["Domain"]]
        :raises ValueError: Cycle found.
        """
        if len(self._subdomains) > 0:
            if seen_domains is None:
                seen_domains = set()

            if self in seen_domains:
                raise ValueError(f"Domain {self} found twice in DFS")
            else:
                seen_domains.add(self)

            for sd in self._subdomains:
                try:
                    sd._check_acyclic_subdomain_graph(seen_domains)
                except ValueError as e:
                    raise ValueError(f"Cycle found in subdomain graph rooted at {self}. "
                                     f"Propogated from subdomain {sd}: {e}"
                                     )

    def all_domains_in_tree(self) -> List["Domain"]:
        domains = self._get_all_domains_from_parent()
        domains.extend(self._get_all_domains_from_this_subtree())
        return domains

    def _get_all_domains_from_parent(self) -> List["Domain"]:
        domains = []

        parent = self.parent
        if parent is not None:
            parent_domains = parent._get_all_domains_from_this_subtree(excluded_subdomain=self)
            domains.extend(parent_domains)
            domains.extend(parent._get_all_domains_from_parent())

        return domains

    def _get_all_domains_from_this_subtree(self, excluded_subdomain: Optional['Domain'] = None) \
            -> List["Domain"]:
        domains = [self]
        for sd in self._subdomains:
            if sd != excluded_subdomain:
                domains.extend(sd._get_all_domains_from_this_subtree())
        return domains

    def has_pool(self) -> bool:
        """
        :return:
            whether a :any:`DomainPool` has been assigned to this :any:`Domain`
        """
        return self._pool is not None


_domains_interned: Dict[str, Domain] = {}


def domains_not_substrings_of_each_other_domain_pair_constraint(
        check_complements: bool = True, short_description: str = 'dom neq', weight: float = 1.0) \
        -> DomainPairConstraint:
    """
    Returns constraint ensuring no two domains are substrings of each other.
    Note that this ensures that no two :any:`Domain`'s are equal if they are the same length.

    :param check_complements: whether to also ensure the check for Watson-Crick complements of the sequences
    :param short_description: short description of constraint suitable for logging to stdout
    :param weight: weight to assign to constraint
    :return: a :any:`DomainPairConstraint` ensuring no two domain sequences contain each other as a substring
             (in particular, if they are equal length, then they are not the same domain)
    """

    # def evaluate(s1: str, s2: str, domain1: Optional[Domain], domain2: Optional[Domain]) -> float:
    def evaluate(seqs: Tuple[str, ...],
                 domains: Optional[Tuple[Domain, Domain]]) -> Tuple[float, str]: # noqa
        s1, s2 = seqs
        if len(s1) > len(s2):
            s1, s2 = s2, s1
        summary = ''
        score = 0.0
        passed = True
        if s1 in s2:
            score = 1.0
            summary = f'{s1} is a substring of {s2}'
            passed = False
        if check_complements:
            # by symmetry, only need to check c1 versus s2 for WC complement, since
            # (s1 not in s2 <==> c1 in c2) and (c1 in s2 <==> s1 in c2)
            c1 = dv.wc(s1)
            if c1 in s2:
                msg = f'{c1} is a substring of {s2}'
                if not passed:
                    summary += f'; {msg}'
                else:
                    summary = msg
                score += 1.0
        return score, summary

    # def summary(domain1: Domain, domain2: Domain) -> str:
    #     s1 = domain1.sequence()
    #     s2 = domain2.sequence()
    #     if len(s1) > len(s2):
    #         s1, s2 = s2, s1
    #     passed = True
    #     result = 'nothing to report'
    #     if s1 in s2:
    #         result = f'{s1} is a substring of {s2}'
    #         passed = False
    #     if check_complements:
    #         c1 = dv.wc(s1)
    #         if c1 in s2:
    #             msg = f'{c1} is a substring of {s2}'
    #             if not passed:
    #                 result += f'; {msg}'
    #             else:
    #                 result = msg
    #             # passed = False
    #     return result

    return DomainPairConstraint(description='domains not substrings of each other',
                                short_description=short_description,
                                weight=weight,
                                _evaluate=evaluate)


@dataclass
class IDTFields(JSONSerializable):
    """Data required when ordering DNA strands from the synthesis company
    `IDT (Integrated DNA Technologies) <https://www.idtdna.com/>`_.
    This data is used when automatically generating files used to order DNA from IDT.

    When exporting to IDT files via :py:meth:`Design.write_idt_plate_excel_file`
    or :py:meth:`Design.write_idt_bulk_input_file`, the field :py:data:`Strand.name` is used for the
    name if it exists, otherwise a reasonable default is chosen."""

    scale: str = default_idt_scale
    """Synthesis scale at which to synthesize the strand (third field in IDT bulk input:
    https://www.idtdna.com/site/order/oligoentry).
    Choices supplied by IDT at the time this was written: 
    ``"25nm"``, ``"100nm"``, ``"250nm"``, ``"1um"``, ``"5um"``, 
    ``"10um"``, ``"4nmU"``, ``"20nmU"``, ``"PU"``, ``"25nmS"``.
    """

    purification: str = default_idt_purification
    """Purification options (fourth field in IDT bulk input:
    https://www.idtdna.com/site/order/oligoentry). 
    Choices supplied by IDT at the time this was written: 
    ``"STD"``, ``"PAGE"``, ``"HPLC"``, ``"IEHPLC"``, ``"RNASE"``, ``"DUALHPLC"``, ``"PAGEHPLC"``.
    """

    plate: Optional[str] = None
    """Name of plate in case this strand will be ordered on a 96-well or 384-well plate.

    Optional field, but non-optional if :data:`IDTFields.well` is not ``None``.
    """

    well: Optional[str] = None
    """Well position on plate in case this strand will be ordered on a 96-well or 384-well plate.

    Optional field, but non-optional if :data:`IDTFields.plate` is not ``None``.
    """

    def __post_init__(self) -> None:
        _check_idt_string_not_none_or_empty(self.scale, 'scale')
        _check_idt_string_not_none_or_empty(self.purification, 'purification')
        if self.plate is None and self.well is not None:
            raise ValueError(f'IDTFields.plate cannot be None if IDTFields.well is not None\n'
                             f'IDTFields.well = {self.well}')
        if self.plate is not None and self.well is None:
            raise ValueError(f'IDTFields.well cannot be None if IDTFields.plate is not None\n'
                             f'IDTFields.plate = {self.plate}')

    def to_json_serializable(self, suppress_indent: bool = True,
                             **kwargs: Any) -> Union[NoIndent, Dict[str, Any]]:
        dct: Dict[str, Any] = dict(self.__dict__)
        if self.plate is None:
            del dct['plate']
        if self.well is None:
            del dct['well']
        return NoIndent(dct)

    @staticmethod
    def from_json(json_map: Dict[str, Any]) -> IDTFields:
        scale = mandatory_field(IDTFields, json_map, idt_scale_key)
        purification = mandatory_field(IDTFields, json_map, idt_purification_key)
        plate = json_map.get(idt_plate_key)
        well = json_map.get(idt_well_key)
        return IDTFields(scale=scale, purification=purification, plate=plate, well=well)

    def clone(self) -> IDTFields:
        return IDTFields(scale=self.scale, purification=self.purification,
                         plate=self.plate, well=self.well)

    def to_scadnano_idt(self) -> sc.IDTFields:
        return sc.IDTFields(scale=self.scale, purification=self.purification,
                            plate=self.plate, well=self.well)


def _check_idt_string_not_none_or_empty(value: str, field_name: str) -> None:
    if value is None:
        raise ValueError(f'field {field_name} in IDTFields cannot be None')
    if len(value) == 0:
        raise ValueError(f'field {field_name} in IDTFields cannot be empty')


default_strand_group = 'default_strand_group'

StrandLabel = TypeVar('StrandLabel')


@dataclass
class Strand(JSONSerializable, Generic[StrandLabel, DomainLabel]):
    """Represents a DNA strand, made of several :any:`Domain`'s. """

    domains: List[Domain[DomainLabel]]
    """The :any:`Domain`'s on this :any:`Strand`, in order from 5' end to 3' end."""

    starred_domain_indices: FrozenSet[int]
    """Set of positions of :any:`Domain`'s in :py:data:`Strand.domains`
    on this :any:`Strand` that are starred."""

    group: str
    """Optional "group" field to describe strands that share similar properties."""

    _domain_names_concatenated: str
    """Concatenation of domain names; cached for efficiency since these are used in calculating 
    hash values."""

    _hash_domain_names_concatenated: int
    """Hash value of _domain_names_concatenated; cached for efficiency."""

    idt: Optional[IDTFields] = None
    """Fields used when ordering strands from the synthesis company IDT 
    (Integrated DNA Technologies, Coralville, IA). If present (i.e., not equal to :const:`None`)
    then the method :py:meth:`Design.write_idt_bulk_input_file` can be called to automatically
    generate an text file for ordering strands in test tubes: 
    https://www.idtdna.com/site/order/oligoentry,
    as can the method :py:meth:`Design.write_idt_plate_excel_file` for writing a Microsoft Excel 
    file that can be uploaded to IDT's website for describing DNA sequences to be ordered in 96-well
    or 384-well plates."""

    _name: Optional[str] = None
    """Optional name of strand."""

    label: Optional[StrandLabel] = None
    """
    Optional generic "label" object to associate to this :any:`Strand`.

    Useful for associating extra information with the :any:`Strand` that will be serialized, for example,
    for DNA sequence design. It must be an object (e.g., a dict or primitive type such as str or int)
    that is naturally JSON serializable. (Calling
    `json.dumps <https://docs.python.org/3/library/json.html#json.dumps>`_
    on the object should succeed without having to specify a custom encoder.)
    """

    def __init__(self,
                 domain_names: Optional[List[str]] = None,
                 domains: Optional[List[Domain[DomainLabel]]] = None,
                 starred_domain_indices: Optional[Iterable[int]] = None,
                 group: str = default_strand_group,
                 name: Optional[str] = None,
                 label: Optional[StrandLabel] = None,
                 idt: Optional[IDTFields] = None,
                 ) -> None:
        """
        A :any:`Strand` can be created either by listing explicit :any:`Domain` objects
        via parameter `domains`, or by giving names via parameter `domain_names`.
        If `domain_names` is specified, then by convention those that end with a ``*`` are
        assumed to be starred. Also, :any:`Domain`'s created in this way are "interned" as global variables;
        no two :any:`Domain`'s with the same name will be created, and subsequent uses of the same
        name will refer to the same :any:`Domain` object.

        :param domain_names:
            Names of the :any:`Domain`'s on this :any:`Strand`.
            Mutually exclusive with :py:data:`Strand.domains` and :py:data:`Strand.starred_domain_indices`.
        :param domains:
            Dictionary mapping each :any:`Domain` on this :any:`Strand` to the Boolean value indicating
            whether it is a starred :any:`Domain`.
            Mutually exclusive with :py:data:`Strand.domain_names`, and must be specified jointly with
            :py:data:`Strand.starred_domain_indices`.
        :param starred_domain_indices:
            Indices of :any:`Domain`'s in `domains` that are starred.
            Mutually exclusive with :py:data:`Strand.domain_names`, and must be specified jointly with
            :py:data:`Strand.domains`.
        :param group:
            name of group of this :any:`Strand`.
        :param name:
            Name of this :any:`Strand`.
        :param label:
            Label to associate with this :any:`Strand`.
        :param idt:
            :any:`IDTFields` object to associate with this :any:`Strand`; needed to call
            methods for exporting to IDT formats (e.g., :meth:`Strand.write_idt_bulk_input_file`)
        """
        self.group = group
        self._name = name
        if (domain_names is not None and not (domains is None and starred_domain_indices is None)) or \
                (domain_names is None and not (domains is not None and starred_domain_indices is not None)):
            raise ValueError('exactly one of domain_names or '
                             'domains and starred_domain_indices must be non-None\n'
                             f'domain_names: {domain_names}\n'
                             f'domains: {domains}\n'
                             f'starred_domain_indices: {starred_domain_indices}')

        elif domain_names is not None:
            domains = []
            starred_domain_indices = OrderedSet()
            for idx, domain_name in enumerate(domain_names):
                is_starred = domain_name.endswith('*')
                if is_starred:
                    domain_name = domain_name[:-1]

                # domain = Domain(name) if name not in _domains_interned else _domains_interned[name]
                domain: Domain
                if domain_name not in _domains_interned:
                    domain = Domain(name=domain_name)
                    _domains_interned[domain_name] = domain
                else:
                    domain = _domains_interned[domain_name]

                domains.append(domain)
                if is_starred:
                    starred_domain_indices.add(idx)

        # Check that each base in the sequence is assigned by exactly one
        # independent subdomain.
        for d in cast(List[Domain], domains):
            d._check_acyclic_subdomain_graph()  # noqa
            d._check_subdomain_graph_is_uniquely_assignable()  # noqa

        self.domains = list(domains)  # type: ignore
        self.starred_domain_indices = frozenset(starred_domain_indices)  # type: ignore
        self.label = label
        self.idt = idt

        self.compute_derived_fields()

    def clone(self, name: Optional[str]) -> Strand:
        """
        Returns a copy of this :any:`Strand`. The copy is "shallow" in that the :any:`Domain`'s are shared.
        This is useful for creating multiple versions of each :any:`Strand`, e.g., for having a
        variant with an extension.

        WARNING: the :data:`Strand.label` will be shared between them. If it should be copied,
        this must be done manually. A shallow copy of it can be made by setting

        :param name:
            new name to give this Strand
        :return:
            A copy of this :any:`Strand`.
        """
        domains = list(self.domains)
        starred_domain_indices = list(self.starred_domain_indices)
        name = name if name is not None else self.name
        idt = None if self.idt is None else self.idt.clone()
        return Strand(domains=domains, starred_domain_indices=starred_domain_indices, name=name,
                      group=self.group, label=self.label, idt=idt)

    def compute_derived_fields(self):
        """
        Re-computes derived fields of this :any:`Strand`. Should be called after modifications to the
        Strand. (Done automatically at the start of :meth:`search.search_for_dna_sequences`.)
        """
        self._domain_names_concatenated = '-'.join(self.domain_names_tuple())
        self._hash_domain_names_concatenated = hash(self._domain_names_concatenated)

    def __hash__(self) -> int:
        return self._hash_domain_names_concatenated

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Strand):
            return False
        return self._domain_names_concatenated == other._domain_names_concatenated

    def length(self) -> int:
        """
        :return:
            Sum of lengths of :any:`Domain`'s in this :any:`Strand`.
            Each :any:`Domain` must have a :any:`DomainPool` assigned so that the length is defined.
        """
        return sum(domain.length for domain in self.domains)

    def domain_names_concatenated(self, delim: str = '-') -> str:
        """
        :param delim:
            Delimiter to put between domain names.
        :return:
            names of :any:`Domain`'s in this :any:`Strand`, concatenated with `delim` in between.
        """
        return delim.join(self.domain_names_tuple())

    def domain_names_tuple(self) -> Tuple[str, ...]:
        """
        :return: tuple of names of :any:`Domain`'s in this :any:`Strand`.
        """
        domain_names: List[str] = []
        for idx, domain in enumerate(self.domains):
            is_starred = idx in self.starred_domain_indices
            domain_names.append(domain.get_name(is_starred))
        return tuple(domain_names)

    def to_json_serializable(self, suppress_indent: bool = True) -> Union[NoIndent, Dict[str, Any]]:
        """
        :return:
            Dictionary ``d`` representing this :any:`Strand` that is "naturally" JSON serializable,
            by calling ``json.dumps(d)``.
        """
        dct: Dict[str, Any] = {name_key: self.name, group_key: self.group}

        domains_list = [domain.name for domain in self.domains]
        dct[domain_names_key] = NoIndent(domains_list) if suppress_indent else domains_list

        starred_domain_indices_list = sorted(list(self.starred_domain_indices))
        dct[starred_domain_indices_key] = NoIndent(starred_domain_indices_list) if suppress_indent \
            else starred_domain_indices_list

        if self.label is not None:
            dct[label_key] = NoIndent(self.label) if suppress_indent else self.label

        if self.idt is not None:
            dct[idt_key] = self.idt.to_json_serializable(suppress_indent)

        return dct

    @staticmethod
    def from_json_serializable(json_map: Dict[str, Any],
                               domain_with_name: Dict[str, Domain[DomainLabel]],
                               label_decoder: Callable[[Any], StrandLabel] = (lambda label: label),
                               ) -> 'Strand[StrandLabel, DomainLabel]':
        """
        :return:
            :any:`Strand` represented by dict `json_map`, assuming it was created by
            :py:meth:`Strand.to_json_serializable`.
        """
        name: str = mandatory_field(Strand, json_map, name_key)
        domain_names_json = mandatory_field(Strand, json_map, domain_names_key)
        domains: List[Domain[DomainLabel]] = [domain_with_name[name] for name in domain_names_json]
        starred_domain_indices = mandatory_field(Strand, json_map, starred_domain_indices_key)

        group = json_map.get(group_key, default_strand_group)

        label_json = json_map.get(label_key)
        label = label_decoder(label_json)

        strand: Strand[StrandLabel, DomainLabel] = Strand(
            domains=domains, starred_domain_indices=starred_domain_indices,
            group=group, name=name, label=label)
        return strand

    def __repr__(self) -> str:
        return self.name

    def unstarred_domains(self) -> List[Domain[DomainLabel]]:
        """
        :return: list of unstarred :any:`Domain`'s in this :any:`Strand`, in order they appear in
                 :py:data:`Strand.domains`
        """
        return [domain for idx, domain in enumerate(self.domains) if idx not in self.starred_domain_indices]

    def starred_domains(self) -> List[Domain[DomainLabel]]:
        """
        :return: list of starred :any:`Domain`'s in this :any:`Strand`, in order they appear in
                 :py:data:`Strand.domains`
        """
        return [domain for idx, domain in enumerate(self.domains) if idx in self.starred_domain_indices]

    def unstarred_domains_set(self) -> OrderedSet[Domain[DomainLabel]]:
        """
        :return: set of unstarred :any:`Domain`'s in this :any:`Strand`
        """
        return OrderedSet(self.unstarred_domains())

    def starred_domains_set(self) -> OrderedSet[Domain[DomainLabel]]:
        """
        :return: set of starred :any:`Domain`'s in this :any:`Strand`
        """
        return OrderedSet(self.starred_domains())

    def sequence(self, delimiter: str = '') -> str:
        """
        :param delimiter:
            Delimiter string to place between sequences of each :any:`Domain` in this :any:`Strand`.
            For instance, if `delimiter` = ``'--'``, then it will return a string such as
            ``ACGTAGCTGA--CGCTAGCTGA--CGATCGATC--GCGATCGAT``
        :return:
            DNA sequence assigned to this :any:`Strand`, calculated by concatenating all sequences
            assigned to its :any:`Domain`'s.
        :raises ValueError:
            if any :any:`Domain` of this :any:`Strand` does not have a sequence assigned
        """
        seqs = []
        for idx, domain in enumerate(self.domains):
            starred = idx in self.starred_domain_indices
            seqs.append(domain.concrete_sequence(starred))
        return delimiter.join(seqs)

    def assign_dna(self, sequence: str) -> None:
        """
        :param sequence:
            DNA sequence to assign to this :any:`Strand`.
            Must have length = :py:meth:`Strand.length`.
        """
        if not self.length() == len(sequence):
            raise ValueError(f'Strand {self.name} has length {self.length()}, but DNA sequence '
                             f'{sequence} has length {len(sequence)}')
        start = 0
        for domain in self.domains:
            end = start + domain.length
            domain_sequence = sequence[start:end]
            domain.set_sequence(domain_sequence)
            start = end

    @property
    def fixed(self) -> bool:
        """True if every :any:`Domain` on this :any:`Strand` has a fixed DNA sequence."""
        return all(domain.fixed for domain in self.domains)

    def unfixed_domains(self) -> List[Domain[DomainLabel]]:
        """
        :return: all :any:`Domain`'s in this :any:`Strand` where :py:data:`Domain.fixed` is False
        """
        return [domain for domain in self.domains if not domain.fixed]

    @property
    def name(self) -> str:
        """
        :return: name of this :any:`Strand` if it was assigned one, otherwise :any:`Domain` names are
                 concatenated with '-' joining them
        """
        return self.domain_names_concatenated() if self._name is None else self._name

    @name.setter
    def name(self, new_name: str) -> None:
        """
        Sets name of this :any:`Strand`.
        """
        self._name = new_name

    # def complementary_domains(self, other: Strand) -> List[Domain]:
    #     """
    #     :param other: another :any:`Strand`
    #     :return: list of :any:`Domain`'s that are complementary between this :any:`Strand` and `other`,
    #              in the order they appear in this :any:`Strand`.
    #     """

    def address_of_domain(self, domain_idx: int) -> 'StrandDomainAddress':
        """Returns :any:`StrandDomainAddress` of the domain located at domain_idx

        :rparam domain_idx: Index of domain
        """
        return StrandDomainAddress(self, domain_idx)

    def address_of_nth_domain_occurence(self, domain_name: str, n: int,
                                        forward=True) -> 'StrandDomainAddress':
        """
        Returns :any:`StrandDomainAddress` of the `n`'th occurence of domain named `domain_name`.

        :param domain_name:
            name of :any:`Domain` to find address of
        :param n:
            which occurrence (in order on the :any:`Strand`)
            of :any:`Domain` with name `domain_name` to find address of.
        :param forward:
            if True, starts searching from 5' end, otherwise starts searching from 3' end.
        :return:
            :any:`StrandDomainAddress` of the `n`'th occurence of domain named `domain_name`.
        """
        if n < 1:
            raise ValueError(f'n needs to be at least 1')
        domain_names = self.domain_names_tuple()
        idx = -1
        occurences = 0

        itr = range(0, len(domain_names)) if forward else range(len(domain_names) - 1, -1, -1)

        for i in itr:
            if domain_names[i] == domain_name:
                occurences += 1
                if occurences == n:
                    idx = i
                    break
        if idx == -1:
            raise ValueError(f'{self} contained less than {n} occurrences of domain {domain_name}')

        return StrandDomainAddress(self, idx)

    def address_of_first_domain_occurence(self, domain_name: str) -> 'StrandDomainAddress':
        """
        Returns :any:`StrandDomainAddress` of the first occurrence of domain named domain_name
        starting from the 5' end.
        """
        return self.address_of_nth_domain_occurence(domain_name, 1)

    def address_of_last_domain_occurence(self, domain_name: str) -> 'StrandDomainAddress':
        """
        Returns :any:`StrandDomainAddress` of the nth occurrence of domain named domain_name
        starting from the 3' end.
        """
        return self.address_of_nth_domain_occurence(domain_name, 1, forward=False)

    def append_domain(self, domain: Domain, starred: bool = False) -> None:
        """
        Appends `domain` to 3' end of this :any:`Strand`.

        :param domain:
            :any:`Domain` to append
        :param starred:
            whether `domain` is starred
        """
        self.insert_domain(len(self.domains), domain, starred)

    def prepend_domain(self, domain: Domain, starred: bool = False) -> None:
        """
        Prepends `domain` to 5' end of this :any:`Strand` (i.e., the beginning of the :any:`Strand`).

        :param domain:
            :any:`Domain` to prepend
        :param starred:
            whether `domain` is starred
        """
        self.insert_domain(0, domain, starred)

    def insert_domain(self, idx: int, domain: Domain, starred: bool = False) -> None:
        """
        Inserts `domain` at index `idx` of this :any:`Strand`, with same semantics as Python's List.insert.
        For example, ``strand.insert(0, domain)`` is equivalent to ``strand.prepend_domain(domain)``
        and ``strand.insert(len(strand.domains), domain)`` is equivalent to ``strand.append_domain(domain)``.

        :param idx:
            index at which to insert `domain` into this :any:`Strand`
        :param domain:
            :any:`Domain` to append
        :param starred:
            whether `domain` is starred
        """
        self.domains.insert(idx, domain)

        new_starred_idx = frozenset([idx]) if starred else frozenset()

        # increment all starred indices >= idx
        starred_domain_indices_at_least_idx = frozenset([idx_
                                                         for idx_ in self.starred_domain_indices
                                                         if idx_ >= idx])
        starred_domain_indices_at_least_idx_inc = frozenset([idx_ + 1
                                                             for idx_ in starred_domain_indices_at_least_idx
                                                             if idx_ >= idx])
        # remove old starred indices >= idx, union in their increments,
        # and if new domain is starred, union it in also
        self.starred_domain_indices = self.starred_domain_indices.difference(
            starred_domain_indices_at_least_idx).union(starred_domain_indices_at_least_idx_inc).union(
            new_starred_idx)


def remove_duplicates(lst: Iterable[T]) -> List[T]:
    """
    :param lst:
        an Iterable of objects
    :return:
        a List consisting of elements of `lst` with duplicates removed,
        while preserving iteration order of `lst`
        (naive approach using Python set would not preserve order,
        since iteration order of Python sets is not specified)
    """
    # XXX: be careful; original version used set to remove duplicates, but that has unspecified
    # insertion order, even though Python 3.7 dicts preserve insertion order:
    # https://softwaremaniacs.org/blog/2020/02/05/dicts-ordered/
    seen: Set[T] = set()
    seen_add = seen.add
    return [x for x in lst if not (x in seen or seen_add(x))]


@dataclass
class ConstraintReport:
    """
    Represents a report on how well a design did on a constraint.
    """

    constraint: Optional['Constraint']
    """
    The :any:`Constraint` to report on. This can be None if the :any:`Constraint` object is not available
    at the time the :py:meth:`Constraint.generate_summary` function is defined. If so it will be
    automatically inserted by the report generating code."""

    content: str
    """
    Summary of constraint information on the :any:`Design`.
    """

    num_violations: int
    """
    Total number of "parts" of the :any:`Design` (e.g., :any:`Strand`'s, pairs of :any:`Domain`'s) that
    violated the constraint.
    """

    num_checks: int
    """
    Total number of "parts" of the :any:`Design` (e.g., :any:`Strand`'s, pairs of :any:`Domain`'s) that
    were checked against the constraint.
    """


def _small_header(header: str, delim: str) -> str:
    width = len(header)
    return f'\n{header}\n{delim * width}'


def _export_dummy_scadnano_design_for_idt_export(strands: Iterable[Strand]) -> sc.Design:
    """
    Exports a dummy scadnano design from this dsd :any:`Design`.
    Useful for reusing scadnano methods such as to_idt_bulk_input_format.

    :param strands:
        strands to export
    :return:
        a "dummy" scadnano design, where domains are positioned arbitrarily on helices,
        with the only goal to make the scadnano Design legal
    """
    helices = [sc.Helix(max_offset=strand.length()) for strand in strands]
    sc_strands = []
    for helix_idx, strand in enumerate(strands):
        idt_export = strand.idt.to_scadnano_idt() if strand.idt is not None else None
        sc_domains = []
        prev_end = 0
        for domain in strand.domains:
            sc_domain = sc.Domain(helix=helix_idx, forward=True,
                                  start=prev_end, end=prev_end + domain.length)
            prev_end = sc_domain.end
            sc_domains.append(sc_domain)
        sc_strand = sc.Strand(domains=sc_domains, idt=idt_export,
                              dna_sequence=strand.sequence(), name=strand.name)
        sc_strands.append(sc_strand)
    design = sc.Design(helices=helices, strands=sc_strands, grid=sc.square)
    return design


_96WELL_PLATE_ROWS: List[str] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']
_96WELL_PLATE_COLS: List[int] = list(range(1, 13))

_384WELL_PLATE_ROWS: List[str] = [
    'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P']
_384WELL_PLATE_COLS: List[int] = list(range(1, 25))


@enum.unique
class PlateType(int, enum.Enum):
    """Represents two different types of plates in which DNA sequences can be ordered."""

    wells96 = 96
    """96-well plate."""

    wells384 = 384
    """384-well plate."""

    def rows(self) -> List[str]:
        return _96WELL_PLATE_ROWS if self is PlateType.wells96 else _384WELL_PLATE_ROWS

    def cols(self) -> List[int]:
        return _96WELL_PLATE_COLS if self is PlateType.wells96 else _384WELL_PLATE_COLS

    def num_wells_per_plate(self) -> int:
        """
        :return:
            number of wells in this plate type
        """
        if self is PlateType.wells96:
            return 96
        elif self is PlateType.wells384:
            return 384
        else:
            raise AssertionError('unreachable')

    def min_wells_per_plate(self) -> int:
        """
        :return:
            minimum number of wells in this plate type to avoid extra charge by IDT
        """
        if self is PlateType.wells96:
            return 24
        elif self is PlateType.wells384:
            return 96
        else:
            raise AssertionError('unreachable')


@dataclass
class Design(Generic[StrandLabel, DomainLabel], JSONSerializable):
    """
    Represents a complete design, i.e., a set of DNA :any:`Strand`'s with domains,
    and :any:`Constraint`'s on the sequences
    to assign to them via :py:meth:`search.search_for_dna_sequences`.
    """

    strands: List[Strand[StrandLabel, DomainLabel]]
    """List of all :any:`Strand`'s in this :any:`Design`."""

    #################################################
    # these fields are calculated from the single constructor parameter constraints

    domain_constraints: List[DomainConstraint] = field(default_factory=list, init=False)
    """
    Applied to individual domain constraints across all :any:`Domain`'s in the :any:`Design`.
    """

    strand_constraints: List[StrandConstraint] = field(default_factory=list, init=False)
    """
    Applied to individual strand constraints across all :any:`Strand`'s in the :any:`Design`.
    """

    domain_pair_constraints: List[DomainPairConstraint] = field(default_factory=list, init=False)
    """
    Applied to pairs of :any:`Domain`'s in the :any:`Design`.
    """

    strand_pair_constraints: List[StrandPairConstraint] = field(default_factory=list, init=False)
    """
    Applied to pairs of :any:`Strand`'s in the :any:`Design`.
    """

    complex_constraints: List[ComplexConstraint] = field(default_factory=list, init=False)
    """
    Applied to tuple of :any:`Strand`'s in the :any:`Design`.
    """

    domains_constraints: List[DomainsConstraint] = field(default_factory=list, init=False)
    """
    Constraints that process all :any:`Domain`'s at once (for example, to hand off in batch to RNAduplex).
    """

    strands_constraints: List[StrandsConstraint] = field(default_factory=list, init=False)
    """
    Constraints that process all :any:`Strand`'s at once (for example, to hand off in batch to RNAduplex).
    """

    domain_pairs_constraints: List[DomainPairsConstraint] = field(default_factory=list, init=False)
    """
    Constraints that process all :any:`Domain`'s at once (for example, to hand off in batch to RNAduplex).
    """

    strand_pairs_constraints: List[StrandPairsConstraint] = field(default_factory=list, init=False)
    """
    Constraints that process all :any:`Strand`'s at once (for example, to hand off in batch to RNAduplex).
    """

    design_constraints: List[DesignConstraint] = field(default_factory=list, init=False)
    """
    Constraints that process whole design at once, for anything not expressible as one of the others
    (for example, in case it needs access to all the :any:`DomainPool`'s at once).
    """

    #################################################
    # derived fields, so not specified in constructor

    domains: List[Domain[DomainLabel]] = field(init=False)
    """
    List of all :any:`Domain`'s in this :any:`Design`. (without repetitions)

    Computed from :py:data:`Design.strands`, so not specified in constructor.
    """

    _strands_by_group_name: Dict[str, List[Strand[StrandLabel, DomainLabel]]] = field(init=False)
    """
    Dict mapping each group name to a list of the :any:`Strand`'s in this :any:`Design` in the group.

    Computed from :py:data:`Design.strands`, so not specified in constructor.
    """

    domain_pools_to_domain_map: Dict[DomainPool, List[Domain]] = field(init=False)
    """
    Dict mapping each :any:`DomainPool` to a list of the :any:`Domain`'s in this :any:`Design` in the pool.

    Computed from :py:data:`Design.strands`, so not specified in constructor.
    """

    domains_by_name: Dict[str, Domain] = field(init=False)
    """
    Dict mapping each name of a :any:`Domain` to the :any:`Domain`'s in this :any:`Design`.

    Computed from :py:data:`Design.strands`, so not specified in constructor.
    """

    def __init__(self, strands: Iterable[Strand],
                 constraints: Optional[Iterable['Constraint']] = None) -> None:
        """
        :param strands:
            the :any:`Strand`'s in this :any:`Design`
        :param constraints:
            the :any:`Constraint`'s to apply to this :any:`Design` when running
            :py:meth:`search.search_for_dna_sequences`
        """
        if constraints is None:
            constraints = []
        self._check_constraint_types(constraints)
        self.strands = strands if isinstance(strands, list) else list(strands)
        self._partition_constraints(constraints)
        self.compute_derived_fields()

    @staticmethod
    def _check_constraint_types(constraints: Iterable['Constraint']) -> None:
        idx = 0
        for constraint in constraints:
            if not isinstance(constraint, Constraint):
                raise ValueError('each element of constraints must be an instance of Constraint, '
                                 f'but the element at index {idx} is of type {type(constraint)}')
            idx += 1

    def add_constraints(self, constraints: Iterable[Constraint]) -> None:
        """
        :param constraints:
            :any:`Constraint`'s to add to this :any:`Design`
        """
        for constraint in constraints:
            if isinstance(constraint, DomainConstraint):
                self.domain_constraints.append(constraint)
            elif isinstance(constraint, StrandConstraint):
                self.strand_constraints.append(constraint)
            elif isinstance(constraint, DomainPairConstraint):
                self.domain_pair_constraints.append(constraint)
            elif isinstance(constraint, StrandPairConstraint):
                self.strand_pair_constraints.append(constraint)
            elif isinstance(constraint, ComplexConstraint):
                self.complex_constraints.append(constraint)
            elif isinstance(constraint, DomainsConstraint):
                self.domains_constraints.append(constraint)
            elif isinstance(constraint, StrandsConstraint):
                self.strands_constraints.append(constraint)
            elif isinstance(constraint, DomainPairsConstraint):
                self.domain_pairs_constraints.append(constraint)
            elif isinstance(constraint, StrandPairsConstraint):
                self.strand_pairs_constraints.append(constraint)
            elif isinstance(constraint, DesignConstraint):
                self.design_constraints.append(constraint)
            else:
                raise ValueError(f'{constraint} is not a valid type of Constraint')

    # sort constraints from constructor constraints parameter into the various types
    def _partition_constraints(self, constraints: Iterable[Constraint]) -> None:
        self.domain_constraints = []
        self.strand_constraints = []
        self.domain_pair_constraints = []
        self.strand_pair_constraints = []
        self.complex_constraints = []
        self.domains_constraints = []
        self.strands_constraints = []
        self.domain_pairs_constraints = []
        self.strand_pairs_constraints = []
        self.design_constraints = []

        self.add_constraints(constraints)

    def compute_derived_fields(self) -> None:
        """
        Computes derived fields of this :any:`Design`. Used to ensure that all fields are valid in case
        the :any:`Design` was manually modified after being created, before running
        :meth:`search.search_for_dna_sequences`.
        """
        # Get domains not explicitly listed on strands that are part of domain tree.
        # Also set up quick access to domain by name, and ensure each domain name unique.
        self.domains_by_name = {}
        domains = []
        for strand in self.strands:
            for domain_in_strand in strand.domains:
                domains_in_tree = domain_in_strand.all_domains_in_tree()
                domains.extend(domains_in_tree)
                for domain_in_tree in domains_in_tree:
                    name = domain_in_tree.name
                    if name in self.domains_by_name and domain_in_tree is not self.domains_by_name[name]:
                        raise ValueError(f'domain names must be unique, '
                                         f'but I found two different domains with name {domain_in_tree.name}')
                    self.domains_by_name[domain_in_tree.name] = domain_in_tree

        self.domains = remove_duplicates(domains)

        self._strands_by_group_name = defaultdict(list)
        for strand in self.strands:
            self._strands_by_group_name[strand.group].append(strand)

        self.store_domain_pools()

        for strand in self.strands:
            strand.compute_derived_fields()

    def to_idt_bulk_input_format(self,
                                 delimiter: str = ',',
                                 key: Optional[KeyFunction[Strand]] = None,
                                 warn_duplicate_name: bool = False,
                                 only_strands_with_idt: bool = False,
                                 strands: Optional[Iterable[Strand]] = None) -> str:
        """Called by :meth:`Design.write_idt_bulk_input_file` to determine what string to write to
        the file. This function can be used to get the string directly without creating a file.

        Parameters have the same meaning as in :meth:`Design.write_idt_bulk_input_file`.

        :return:
            string that is written to the file in the method :meth:`Design.write_idt_bulk_input_file`.
        """
        if strands is None:
            strands = self.strands
        sc_design = _export_dummy_scadnano_design_for_idt_export(strands)
        return sc_design.to_idt_bulk_input_format(delimiter, key, warn_duplicate_name, only_strands_with_idt)

    def write_idt_bulk_input_file(self, *, directory: str = '.', filename: str = None,
                                  key: Optional[KeyFunction[Strand]] = None,
                                  extension: Optional[str] = None,
                                  delimiter: str = ',',
                                  warn_duplicate_name: bool = True,
                                  only_strands_with_idt: bool = False,
                                  strands: Optional[Iterable[Strand]] = None) -> None:
        """Write ``.idt`` text file encoding the strands of this :any:`Design` with the field
        :any:`Strand.idt`, suitable for pasting into the "Bulk Input" field of IDT
        (Integrated DNA Technologies, Coralville, IA, https://www.idtdna.com/),
        with the output file having the same name as the running script but with ``.py`` changed to ``.idt``,
        unless `filename` is explicitly specified.
        For instance, if the script is named ``my_origami.py``,
        then the sequences will be written to ``my_origami.idt``.
        If `filename` is not specified but `extension` is, then that extension is used instead of ``idt``.
        At least one of `filename` or `extension` must be ``None``.

        The string written is that returned by :meth:`Design.to_idt_bulk_input_format`.

        :param directory:
            specifies a directory in which to place the file, either absolute or relative to
            the current working directory. Default is the current working directory.
        :param filename:
            optional custom filename to use (instead of currently running script)
        :param key:
            `key function <https://docs.python.org/3/howto/sorting.html#key-functions>`_ used to determine
            order in which to output strand sequences. Some useful defaults are provided by
            :meth:`strand_order_key_function`
        :param extension:
            alternate filename extension to use (instead of idt)
        :param delimiter:
            is the symbol to delimit the four IDT fields name,sequence,scale,purification.
        :param warn_duplicate_name:
            if ``True`` prints a warning when two different :any:`Strand`'s have the same
            :data:`IDTFields.name` and the same :meth:`Strand.sequence`. A ValueError
            is raised (regardless of the value of this parameter)
            if two different :any:`Strand`'s have the same name but different sequences, IDT scales, or IDT
            purifications.
        :param only_strands_with_idt:
            If False (the default), all non-scaffold sequences are output, with reasonable default values
            chosen if the field :data:`Strand.idt` is missing.
            If True, then strands lacking the field :data:`Strand.idt` will not be exported.
        :param strands:
            strands to export; if not specified, all strands in design are exported.
            NOTE: it is not checked that each :any:`Strand` in `strands` is actually contained in this
            any:`Design`
        """
        contents = self.to_idt_bulk_input_format(delimiter=delimiter,
                                                 key=key,
                                                 warn_duplicate_name=warn_duplicate_name,
                                                 only_strands_with_idt=only_strands_with_idt,
                                                 strands=strands)
        if extension is None:
            extension = 'idt'
        sc.write_file_same_name_as_running_python_script(contents, extension, directory, filename)

    def write_idt_plate_excel_file(self, *, directory: str = '.', filename: str = None,
                                   key: Optional[KeyFunction[Strand]] = None,
                                   warn_duplicate_name: bool = False,
                                   only_strands_with_idt: bool = False,
                                   use_default_plates: bool = True, warn_using_default_plates: bool = True,
                                   plate_type: PlateType = PlateType.wells96,
                                   strands: Optional[Iterable[Strand]] = None) -> None:
        """
        Write ``.xls`` (Microsoft Excel) file encoding the strands of this :any:`Design` with the field
        :py:data:`Strand.idt`, suitable for uploading to IDT
        (Integrated DNA Technologies, Coralville, IA, https://www.idtdna.com/)
        to describe a 96-well or 384-well plate
        (https://www.idtdna.com/site/order/plate/index/dna/),
        with the output file having the same name as the running script but with ``.py`` changed to ``.xls``,
        unless `filename` is explicitly specified.
        For instance, if the script is named ``my_origami.py``,
        then the sequences will be written to ``my_origami.xls``.

        If the last plate as fewer than 24 strands for a 96-well plate, or fewer than 96 strands for a
        384-well plate, then the last two plates are rebalanced to ensure that each plate has at least
        that number of strands, because IDT charges extra for a plate with too few strands:
        https://www.idtdna.com/pages/products/custom-dna-rna/dna-oligos/custom-dna-oligos

        :param directory:
            specifies a directory in which to place the file, either absolute or relative to
            the current working directory. Default is the current working directory.
        :param filename:
            custom filename if default (explained above) is not desired
        :param key:
            `key function <https://docs.python.org/3/howto/sorting.html#key-functions>`_ used to determine
            order in which to output strand sequences. Some useful defaults are provided by
            :meth:`strand_order_key_function`
        :param warn_duplicate_name:
            if ``True`` prints a warning when two different :any:`Strand`'s have the same
            :data:`IDTFields.name` and the same :meth:`Strand.sequence`. A ValueError is
            raised (regardless of the value of this parameter)
            if two different :any:`Strand`'s have the same name but different sequences, IDT scales, or IDT
            purifications.
        :param only_strands_with_idt:
            If False (the default), all non-scaffold sequences are output, with reasonable default values
            chosen if the field :data:`Strand.idt` is missing.
            (though scaffold is included if `export_scaffold` is True).
            If True, then strands lacking the field :data:`Strand.idt` will not be exported.
            If False, then `use_default_plates` must be True.
        :param use_default_plates:
            Use default values for plate and well (ignoring those in idt fields, which may be None).
            If False, each Strand to export must have the field :data:`Strand.idt`, so in particular
            the parameter `only_strands_with_idt` must be True.
        :param warn_using_default_plates:
            specifies whether, if `use_default_plates` is True, to print a warning for strands whose
            :data:`Strand.idt` has the fields :py:data:`IDTFields.plate` and :py:data:`IDTFields.well`,
            since `use_default_plates` directs these fields to be ignored.
        :param plate_type:
            a :any:`PlateType` specifying whether to use a 96-well plate or a 384-well plate
            if the `use_default_plates` parameter is ``True``.
            Ignored if `use_default_plates` is ``False``, because in that case the wells are explicitly set
            by the user, who is free to use coordinates for either plate type.
        :param strands:
            strands to export; if not specified, all strands in design are exported.
            NOTE: it is not checked that each :any:`Strand` in `strands` is actually contained in this
            any:`Design`
        """
        if strands is None:
            strands = self.strands
        sc_design = _export_dummy_scadnano_design_for_idt_export(strands)
        sc_design.write_idt_plate_excel_file(directory=directory,
                                             filename=filename,
                                             key=key,
                                             warn_duplicate_name=warn_duplicate_name,
                                             only_strands_with_idt=only_strands_with_idt,
                                             use_default_plates=use_default_plates,
                                             warn_using_default_plates=warn_using_default_plates,
                                             plate_type=plate_type)

    def store_domain_pools(self) -> None:
        self.domain_pools_to_domain_map = defaultdict(list)
        for domain in self.domains:
            if domain._pool is not None:  # noqa
                self.domain_pools_to_domain_map[domain.pool].append(domain)

    def domain_pools(self) -> List[DomainPool]:
        """
        :return:
            list of all :any:`DomainPool`'s in this :any:`Design`
        """
        return list(self.domain_pools_to_domain_map.keys())

    def to_json(self) -> str:
        """
        :return:
            JSON string representing this :any:`Design`.
        """
        self.store_domain_pools()
        return json_encode(self, suppress_indent=True)

    @staticmethod
    def from_json(json_str: str,
                  pool_with_name: Optional[Dict[str, DomainPool]] = None,
                  strand_label_decoder: Callable[[Any], StrandLabel] = lambda label: label,
                  domain_label_decoder: Callable[[Any], DomainLabel] = lambda label: label,
                  ) -> 'Design[StrandLabel, DomainLabel]':
        """
        :param json_str:
            The string representing the :any:`Design` as a JSON object.
        :param pool_with_name:
            If specified should map a name to the :any:`DomainPool` with that name.
        :param domain_label_decoder:
            Function that transforms JSON representation of :py:data:`Domain.label` into the proper type.
        :param strand_label_decoder:
            Function that transforms JSON representation of :py:data:`Strand.label` into the proper type.
        :return:
            :any:`Design` described by this JSON string, assuming it was created using
            :py:meth`Design.to_json`.
        """
        json_map = json.loads(json_str)
        design: Design[StrandLabel, DomainLabel] = Design.from_json_serializable(
            json_map, pool_with_name=pool_with_name,
            domain_label_decoder=domain_label_decoder, strand_label_decoder=strand_label_decoder)
        return design

    def to_json_serializable(self, suppress_indent: bool = True) -> Dict[str, Any]:
        """
        :param suppress_indent:
            Whether to suppress indentation of some objects using the NoIndent object.
        :return:
            Dictionary ``d`` representing this :any:`Design` that is "naturally" JSON serializable,
            by calling ``json.dumps(d)``.
        """
        return {
            strands_key: [strand.to_json_serializable(suppress_indent) for strand in self.strands],
            domains_key: [domain.to_json_serializable(suppress_indent) for domain in self.domains],
            domain_pools_num_sampled_key: {pool.name: pool.num_sampled for pool in self.domain_pools()}
        }

    @staticmethod
    def from_json_serializable(json_map: Dict[str, Any],
                               pool_with_name: Optional[Dict[str, DomainPool]] = None,
                               domain_label_decoder: Callable[[Any], DomainLabel] = lambda label: label,
                               strand_label_decoder: Callable[[Any], StrandLabel] = lambda label: label,
                               ) -> 'Design[StrandLabel, DomainLabel]':
        """
        :param json_map:
            JSON serializable object encoding this :any:`Design`, as returned by
            :py:meth:`Design.to_json_serializable`.
        :param pool_with_name:
            dict mapping name to :any:`DomainPool` with that name; required to rehydrate :any:`Domain`'s.
            If None, then a DomainPool with no constraints is created with the name and domain length
            found in the JSON.
        :param domain_label_decoder:
            Function that transforms JSON representation of :py:data:`Domain.label` into the proper type.
        :param strand_label_decoder:
            Function that transforms JSON representation of :py:data:`Strand.label` into the proper type.
        :return:
            :any:`Design` represented by dict `json_map`, assuming it was created by
            :py:meth:`Design.to_json_serializable`. No constraints are populated.
        """
        domains_json = mandatory_field(Design, json_map, domains_key)
        domains: List[Domain] = [
            Domain.from_json_serializable(domain_json, pool_with_name=pool_with_name,
                                          label_decoder=domain_label_decoder)
            for domain_json in domains_json]
        domain_with_name = {domain.name: domain for domain in domains}

        strands_json = mandatory_field(Design, json_map, strands_key)
        strands = [Strand.from_json_serializable(
            json_map=strand_json, domain_with_name=domain_with_name,
            label_decoder=strand_label_decoder)
            for strand_json in strands_json]

        return Design(strands=strands)

    def strands_by_group_name(self, group: str) -> List[Strand[StrandLabel, DomainLabel]]:
        """
        :param group: name of a group
        :return: list of :any:`Strand`'s in that group
        """
        return self._strands_by_group_name[group]

    def domains_by_pool_name(self, domain_pool_name: str) -> List[Domain[DomainLabel]]:
        """
        :param domain_pool_name: name of a :any:`DomainPool`
        :return: the :any:`Domain`'s in `domain_pool`
        """
        domains_in_pool: List[Domain] = []
        for domain in self.domains:
            if domain.pool.name == domain_pool_name:
                domains_in_pool.append(domain)
        return domains_in_pool

    def copy_constraints_from(self, design: Design) -> None:
        self.domain_constraints = design.domain_constraints
        self.strand_constraints = design.strand_constraints
        self.domain_pair_constraints = design.domain_pair_constraints
        self.strand_pair_constraints = design.strand_pair_constraints
        self.domains_constraints = design.domains_constraints
        self.strands_constraints = design.strands_constraints
        self.domain_pairs_constraints = design.domain_pairs_constraints
        self.strand_pairs_constraints = design.strand_pairs_constraints
        self.complex_constraints = design.complex_constraints
        self.design_constraints = design.design_constraints

    def all_constraints(self) -> List[Constraint]:
        # Since list types are covariant, we cannot use + to concatenate them without upsetting mypy:
        # https://stackoverflow.com/questions/56738485/why-do-i-get-a-warning-when-concatenating-lists-of-mixed-types-in-pycharm
        # https://github.com/python/mypy/issues/4244
        # https://mypy.readthedocs.io/en/latest/common_issues.html#invariance-vs-covariance
        constraints: List[Constraint] = []
        constraints.extend(self.domain_constraints)
        constraints.extend(self.strand_constraints)
        constraints.extend(self.domain_pair_constraints)
        constraints.extend(self.strand_pair_constraints)
        constraints.extend(self.domains_constraints)
        constraints.extend(self.strands_constraints)
        constraints.extend(self.domain_pairs_constraints)
        constraints.extend(self.strand_pairs_constraints)
        constraints.extend(self.complex_constraints)
        constraints.extend(self.design_constraints)
        return constraints

    def summary_of_constraints(self, report_only_violations: bool, violation_set: ViolationSet) -> str:
        summaries: List[str] = []

        # other constraints
        for constraint in self.all_constraints():
            summary = self.summary_of_constraint(constraint, report_only_violations, violation_set)
            summaries.append(summary)

        score = violation_set.total_score()
        score_unfixed = violation_set.total_nonfixed_score()
        score_total_summary = f'total score of constraint violations: {score:.1f}'
        score_unfixed_summary = f'total score of unfixed constraint violations: {score_unfixed:.1f}'

        summary = score_total_summary + '\n'
        if score_unfixed != score:
            summary += score_unfixed_summary + '\n\n'
        else:
            summary += '\n'

        return summary + '\n'.join(summaries)

    def summary_of_constraint(self, constraint: Constraint, report_only_violations: bool,
                              violation_set: ViolationSet) -> str:
        # summary of constraint only if not a DomainConstraint in a DomainPool
        # or a StrandConstraint in a StrandGroup
        report: ConstraintReport
        content: str
        num_violations: int
        num_checks: int
        if isinstance(constraint, DomainConstraint):
            report = self.summary_of_domain_constraint(constraint, report_only_violations)
        elif isinstance(constraint, StrandConstraint):
            report = self.summary_of_strand_constraint(constraint, report_only_violations)
        elif isinstance(constraint, DomainPairConstraint):
            report = self.summary_of_domain_pair_constraint(constraint, report_only_violations)
        elif isinstance(constraint, StrandPairConstraint):
            report = self.summary_of_strand_pair_constraint(constraint, report_only_violations)
        elif isinstance(constraint, DomainsConstraint):
            report = self.summary_of_domains_constraint(constraint, report_only_violations)
        elif isinstance(constraint, StrandsConstraint):
            report = self.summary_of_strands_constraint(constraint, report_only_violations)
        elif isinstance(constraint, DomainPairsConstraint):
            report = self.summary_of_domain_pairs_constraint(constraint, report_only_violations)
        elif isinstance(constraint, StrandPairsConstraint):
            report = self.summary_of_strand_pairs_constraint(constraint, report_only_violations)
        elif isinstance(constraint, DesignConstraint):
            report = self.summary_of_design_constraint(constraint, report_only_violations)
        elif isinstance(constraint, ComplexConstraint):
            report = self.summary_of_complex_constraint(constraint, report_only_violations)
        else:
            content = f'skipping summary of constraint {constraint.description}; ' \
                      f'unrecognized type {type(constraint)}'
            report = ConstraintReport(constraint=constraint, content=content, num_violations=0, num_checks=0)

        report.constraint = constraint

        if _no_summary_string in report.content:
            report = ConstraintReport(constraint=constraint, content=_no_summary_string,
                                      num_violations=0, num_checks=0)

        summary = add_header_to_content_of_summary(report, violation_set)
        return summary

    def summary_of_domain_constraint(self, constraint: DomainConstraint, report_only_violations: bool) \
            -> ConstraintReport:
        num_violations = 0
        num_checks = 0
        domains_to_check = self.domains if constraint.domains is None else constraint.domains
        max_domain_name_length = max(len(domain.name) for domain in domains_to_check)

        summaries = []

        fixed_domains = [domain for domain in domains_to_check if domain.fixed]
        unfixed_domains = [domain for domain in domains_to_check if not domain.fixed]
        for domains_to_check, header_name in [(unfixed_domains, 'unfixed domains'),
                                              (fixed_domains, 'fixed domains')]:
            if len(domains_to_check) > 0:
                report = self._summary_of_domains_in_domain_constraint(
                    constraint, report_only_violations, domains_to_check, max_domain_name_length)
                summary = _small_header(header_name, "=") + f'\n{report.content}\n'
                num_violations += report.num_violations
                num_checks += report.num_checks
                summaries.append(summary)

        content = ''.join(summaries)
        report = ConstraintReport(constraint=constraint, content=content,
                                  num_violations=num_violations, num_checks=num_checks)
        return report

    # this function reuses code between summarizing fixed and unfixed domains
    @staticmethod
    def _summary_of_domains_in_domain_constraint(constraint: DomainConstraint,
                                                 report_only_violations: bool,
                                                 domains: Iterable[Domain[DomainLabel]],
                                                 max_domain_name_length: int) -> ConstraintReport:
        num_violations = 0
        num_checks = 0
        lines_and_excesses: List[Tuple[str, float]] = []
        for domain in domains:
            num_checks += 1
            seq = domain.sequence()
            excess, summary = constraint.evaluate((seq,), domain)
            passed = excess <= 0.0
            if not passed:
                num_violations += 1
            if not report_only_violations or (report_only_violations and not passed):
                # summary = constraint.generate_summary(fixed_domain, False)
                line = f'domain {domain.name:{max_domain_name_length}}: ' \
                       f'{summary} ' \
                       f'{"" if passed else " **violation**"}'
                lines_and_excesses.append((line, excess))

        # put in descending order of excess
        lines_and_excesses.sort(key=lambda line_and_excess: line_and_excess[1], reverse=True)

        lines = (line for line, _ in lines_and_excesses)
        content = '\n'.join(lines)
        report = ConstraintReport(constraint=constraint, content=content,
                                  num_violations=num_violations, num_checks=num_checks)
        return report

    def summary_of_strand_constraint(self, constraint: StrandConstraint, report_only_violations: bool) \
            -> ConstraintReport:
        num_violations = 0
        num_checks = 0
        all_strands_to_check = self.strands if constraint.strands is None else constraint.strands
        max_strand_name_length = max(len(strand.name) for strand in all_strands_to_check)

        summaries = []

        fixed_strands = [strand for strand in all_strands_to_check if strand.fixed]
        unfixed_strands = [strand for strand in all_strands_to_check if not strand.fixed]
        for strands_to_check, header_name in [(unfixed_strands, 'unfixed strands'),
                                              (fixed_strands, 'fixed strands')]:
            if len(strands_to_check) > 0:
                report = self._summary_of_strands_in_strand_constraint(
                    constraint, report_only_violations, strands_to_check, max_strand_name_length)
                summary = _small_header(header_name, "=") + f'\n{report.content}\n'
                num_violations += report.num_violations
                num_checks += report.num_checks
                summaries.append(summary)

        content = ''.join(summaries)
        report = ConstraintReport(constraint=constraint, content=content,
                                  num_violations=num_violations, num_checks=num_checks)
        return report

    # this function reuses code between summarizing fixed and unfixed strands
    @staticmethod
    def _summary_of_strands_in_strand_constraint(constraint: StrandConstraint,
                                                 report_only_violations: bool,
                                                 strands: Iterable[Strand[StrandLabel, DomainLabel]],
                                                 max_strand_name_length: int) -> ConstraintReport:
        num_violations = 0
        num_checks = 0
        lines_and_excesses: List[Tuple[str, float]] = []
        for strand in strands:
            num_checks += 1
            # excess = constraint(strand.sequence(), strand)
            seq = strand.sequence()
            excess, summary = constraint.evaluate((seq,), strand)
            passed = excess <= 0.0
            if not passed:
                num_violations += 1
            if not report_only_violations or (report_only_violations and not passed):
                # summary = constraint.generate_summary(strand, False)
                line = f'strand {strand.name:{max_strand_name_length}}: ' \
                       f'{summary} ' \
                       f'{"" if passed else " **violation**"}'
                lines_and_excesses.append((line, excess))

        # put in descending order of excess
        lines_and_excesses.sort(key=lambda line_and_excess: line_and_excess[1], reverse=True)

        lines = (line for line, _ in lines_and_excesses)
        content = '\n'.join(lines)
        report = ConstraintReport(constraint=constraint, content=content,
                                  num_violations=num_violations, num_checks=num_checks)
        return report

    def summary_of_domain_pair_constraint(self, constraint: DomainPairConstraint,
                                          report_only_violations: bool) -> ConstraintReport:
        pairs_to_check = constraint.pairs if constraint.pairs is not None else all_pairs(self.domains)

        max_domain_name_length = max(len(domain.name) for domain in _flatten(pairs_to_check))

        num_violations = 0
        num_checks = 0
        lines_and_excesses: List[Tuple[str, float]] = []
        for domain1, domain2 in pairs_to_check:
            num_checks += 1
            seq1, seq2 = domain1.sequence, domain2.sequence
            # excess = constraint(seq1, seq2, domain1, domain2)
            excess, summary = constraint.evaluate((seq1, seq2), (domain1, domain2))
            passed = excess <= 0.0
            if not passed:
                num_violations += 1
            if not report_only_violations or (report_only_violations and not passed):
                # summary = constraint.generate_summary((domain1, domain2), False)
                line = (f'domains '
                        f'{domain1.name:{max_domain_name_length}}, '
                        f'{domain2.name:{max_domain_name_length}}: '
                        f'{summary}'
                        f'{"" if passed else "  **violation**"}')
                lines_and_excesses.append((line, excess))

        # put in descending order of excess
        lines_and_excesses.sort(key=lambda line_and_excess: line_and_excess[1], reverse=True)

        lines = (line for line, _ in lines_and_excesses)
        content = '\n'.join(lines)
        report = ConstraintReport(constraint=constraint, content=content,
                                  num_violations=num_violations, num_checks=num_checks)
        return report

    def summary_of_strand_pair_constraint(self, constraint: StrandPairConstraint,
                                          report_only_violations: bool) -> ConstraintReport:
        num_violations = 0
        num_checks = 0
        all_pairs_to_check: List[
            Tuple[Strand, Strand]] = constraint.pairs if constraint.pairs is not None else all_pairs(
            self.strands)

        # distinguish between pairs in which both strands are fixed (so cannot remove violation)
        # versus those pairs in which at least one element of the pair is unfixed
        both_fixed_pairs = [(s1, s2) for s1, s2 in all_pairs_to_check if s1.fixed and s2.fixed]
        one_unfixed_pairs = [(s1, s2) for s1, s2 in all_pairs_to_check if not (s1.fixed and s2.fixed)]

        max_strand_name_length = max(len(strand.name) for strand in _flatten(all_pairs_to_check))

        summaries = []

        for pairs_to_check, header_name in [(one_unfixed_pairs, 'pairs with at least one unfixed'),
                                            (both_fixed_pairs, 'pairs with both fixed')]:
            if len(pairs_to_check) > 0:
                report = self._summary_of_strand_pairs_in_strand_pair_constraint(
                    constraint, report_only_violations, pairs_to_check, max_strand_name_length)
                summary = _small_header(header_name, "=") + f'\n{report.content}\n'
                num_violations += report.num_violations
                num_checks += report.num_checks
                summaries.append(summary)

        content = ''.join(summaries)
        report = ConstraintReport(constraint=constraint, content=content,
                                  num_violations=num_violations, num_checks=num_checks)
        return report

    # this function reuses code between summarizing fixed and unfixed strands
    @staticmethod
    def _summary_of_strand_pairs_in_strand_pair_constraint(constraint: StrandPairConstraint,
                                                           report_only_violations: bool,
                                                           pairs: Iterable[
                                                               Tuple[Strand[StrandLabel, DomainLabel],
                                                                     Strand[StrandLabel, DomainLabel]]],
                                                           max_strand_name_length: int) -> ConstraintReport:
        num_violations = 0
        num_checks = 0
        lines_and_excesses: List[Tuple[str, float]] = []
        for strand1, strand2 in pairs:
            num_checks += 1
            # excess = constraint(strand1.sequence(), strand2.sequence(), strand1, strand2)
            excess, summary = constraint.evaluate((strand1.sequence(), strand2.sequence()), (strand1, strand2))
            passed = excess <= 0.0
            if not passed:
                num_violations += 1
            if not report_only_violations or (report_only_violations and not passed):
                # summary = constraint.generate_summary((strand1, strand2), False)
                line = (f'strands '
                        f'{strand1.name:{max_strand_name_length}}, '
                        f'{strand2.name:{max_strand_name_length}}: '
                        f'{summary}'
                        f'{"" if passed else "  **violation**"}')
                lines_and_excesses.append((line, excess))

        # put in descending order of excess
        lines_and_excesses.sort(key=lambda line_and_excess: line_and_excess[1], reverse=True)

        lines = (line for line, _ in lines_and_excesses)
        content = '\n'.join(lines)
        report = ConstraintReport(constraint=constraint, content=content,
                                  num_violations=num_violations, num_checks=num_checks)
        return report

    @staticmethod
    def summary_of_complex_constraint(constraint: 'ComplexConstraint',
                                      report_only_violations: bool) -> ConstraintReport:
        num_violations = 0
        num_checks = 0

        lines_and_excesses: List[Tuple[str, float]] = []
        for strand_complex in constraint.complexes:
            num_checks += 1
            excess = constraint(strand_complex)
            passed = excess <= 0.0
            if not passed:
                num_violations += 1
            if not report_only_violations or (report_only_violations and not passed):
                summary = constraint.generate_summary(strand_complex, False)
                strand_names = ', '.join([f'{strand.name}' for strand in strand_complex])
                line = (f'strand complex: '
                        f'{strand_names}'
                        f'{"" if passed else "  **violation**"}'
                        f'\n{summary}')
                lines_and_excesses.append((line, excess))

        # put in descending order of excess
        lines_and_excesses.sort(key=lambda line_and_excess: line_and_excess[1], reverse=True)

        lines = (line for line, _ in lines_and_excesses)
        content = '\n'.join(lines)
        report = ConstraintReport(constraint=constraint, content=content,
                                  num_violations=num_violations, num_checks=num_checks)
        return report

    def summary_of_domains_constraint(self, constraint: DomainsConstraint,
                                      report_only_violations: bool) -> ConstraintReport:
        # summary = f'domains\n{constraint.generate_summary(self.domains)}'
        report = constraint.generate_summary(self.domains, report_only_violations)
        return report

    def summary_of_strands_constraint(self, constraint: StrandsConstraint,
                                      report_only_violations: bool) -> ConstraintReport:
        # summary = f'strands\n{constraint.generate_summary(self.strands)}'
        report = constraint.generate_summary(self.strands, report_only_violations)
        return report

    def summary_of_domain_pairs_constraint(self, constraint: DomainPairsConstraint,
                                           report_only_violations: bool) -> ConstraintReport:
        pairs_to_check = constraint.pairs if constraint.pairs is not None else all_pairs(self.domains)
        # summary = f'domain pairs\n{constraint.generate_summary(pairs_to_check)}'
        report = constraint.generate_summary(pairs_to_check, report_only_violations) \
            if len(pairs_to_check) > 0 \
            else ConstraintReport(constraint=constraint,
                                  content='constraint.pairs is empty; nothing to report',
                                  num_violations=0, num_checks=0)
        return report

    def summary_of_strand_pairs_constraint(self, constraint: StrandPairsConstraint,
                                           report_only_violations: bool) -> ConstraintReport:
        pairs_to_check = constraint.pairs if constraint.pairs is not None else all_pairs(self.strands)
        report = constraint.generate_summary(pairs_to_check, report_only_violations) \
            if len(pairs_to_check) > 0 \
            else ConstraintReport(constraint=constraint,
                                  content='constraint.pairs is empty; nothing to report',
                                  num_violations=0, num_checks=0)
        return report

    def summary_of_design_constraint(self, constraint: DesignConstraint,
                                     report_only_violations: bool) -> ConstraintReport:
        # summary = f'design\n{constraint.generate_summary(self)}'
        report = constraint.generate_summary(self, report_only_violations)
        return report

    @staticmethod
    def from_scadnano_file(sc_filename: str,
                           fix_assigned_sequences: bool = True,
                           ignored_strands: Optional[Iterable] = None) -> Design[StrandLabel, DomainLabel]:
        """
        Converts a scadnano Design stored in file named `sc_filename` to a a :any:`Design` for doing
        DNA sequence design.
        Each Strand name and Domain name from the scadnano Design are assigned as
        :py:data:`Strand.name` and :py:data:`Domain.name` in the obvious way.
        Assumes each Strand label is a string describing the strand group.

        The scadnano package must be importable.

        Also assigns sequences from domains in sc_design to those of the returned :any:`Design`.
        If `fix_assigned_sequences` is true, then these DNA sequences are fixed; otherwise not.

        :param sc_filename:
            Name of file containing scadnano Design.
        :param fix_assigned_sequences:
            Whether to fix the sequences that are assigned from those found in `sc_design`.
        :param ignored_strands:
            Strands to ignore
        :return:
            An equivalent :any:`Design`, ready to be given constraints for DNA sequence design.
        :raises TypeError:
            If any scadnano strand label is not a string.
        """
        sc_design = sc.Design.from_scadnano_file(sc_filename)
        return Design.from_scadnano_design(sc_design, fix_assigned_sequences, ignored_strands)

    @staticmethod
    def from_scadnano_design(sc_design: sc.Design[StrandLabel, DomainLabel],
                             fix_assigned_sequences: bool = True,
                             ignored_strands: Optional[Iterable] = None,
                             warn_existing_domain_labels: bool = True) -> Design[StrandLabel, DomainLabel]:
        """
        Converts a scadnano Design `sc_design` to a a :any:`Design` for doing DNA sequence design.
        Each Strand name and Domain name from the scadnano Design are assigned as
        :py:data:`Strand.name` and :py:data:`Domain.name` in the obvious way.
        Assumes each Strand label is a string describing the strand group.

        The scadnano package must be importable.

        Also assigns sequences from domains in sc_design to those of the returned :any:`Design`.
        If `fix_assigned_sequences` is true, then these DNA sequences are fixed; otherwise not.

        :param sc_design:
            Instance of scadnano.Design from the scadnano Python scripting library.
        :param fix_assigned_sequences:
            Whether to fix the sequences that are assigned from those found in `sc_design`.
        :param ignored_strands:
            Strands to ignore; none are ignore if not specified.
        :param warn_existing_domain_labels:
            If True, logs warning when dsd :any:`Domain` already has a label and so does scadnano domain,
            since scadnano label will not be assigned to the dsd :any:`Domain`.
        :return:
            An equivalent :any:`Design`, ready to be given constraints for DNA sequence design.
        :raises TypeError:
            If any scadnano strand label is not a string.
        """

        # check types
        if not isinstance(sc_design, sc.Design):
            raise TypeError(f'sc_design must be an instance of scadnano.Design, but it is {type(sc_design)}')
        if ignored_strands is not None:
            for ignored_strand in ignored_strands:
                if not isinstance(ignored_strand, sc.Strand):
                    raise TypeError('each ignored strand must be an instance of scadnano.Strand, but one is '
                                    f'{type(ignored_strand)}: {ignored_strand}')

        # filter out ignored strands
        strands_to_include = [strand for strand in sc_design.strands if strand not in ignored_strands] \
            if ignored_strands is not None else sc_design.strands

        # warn if not labels are dicts containing group_name_key on strands
        for sc_strand in strands_to_include:
            if (isinstance(sc_strand.label, dict) and group_key not in sc_strand.label) or \
                    (not isinstance(sc_strand.label, dict) and not hasattr(sc_strand.label, group_key)):
                logger.warning(f'Strand label {sc_strand.label} should be an object with attribute named '
                               f'"{group_key}" (for instance a dict or namedtuple).\n'
                               f'  The label is type {type(sc_strand.label)}. '
                               f'In order to auto-populate StrandGroups, ensure the label has attribute '
                               f'named "{group_key}" with associated value of type str.')
            else:
                label_value = Design.get_group_name_from_strand_label(sc_strand)
                if not isinstance(label_value, str):
                    logger.warning(f'Strand label {sc_strand.label} has attribute named '
                                   f'"{group_key}", but its associated value is not a string.\n'
                                   f'The value is type {type(label_value)}. '
                                   f'In order to auto-populate StrandGroups, ensure the label has attribute '
                                   f'named "{group_key}" with associated value of type str.')

                # raise TypeError(f'strand label {sc_strand.label} must be a dict, '
                #                 f'but instead is type {type(sc_strand.label)}')

        # groups scadnano strands by strand labels
        sc_strand_groups: DefaultDict[str, List[sc.Strand]] = defaultdict(list)
        for sc_strand in strands_to_include:
            assigned = False
            if hasattr(sc_strand.label, group_key) or (
                    isinstance(sc_strand.label, dict) and group_key in sc_strand.label):
                group = Design.get_group_name_from_strand_label(sc_strand)
                if isinstance(group, str):
                    sc_strand_groups[group].append(sc_strand)
                    assigned = True
            if not assigned:
                sc_strand_groups[default_strand_group].append(sc_strand)

        # make dsd StrandGroups, taking names from Strands and Domains,
        # and assign (and maybe fix) DNA sequences
        dsd_strands: List[Strand] = []
        strand_names: Set[str] = set()
        for group, sc_strands in sc_strand_groups.items():
            for sc_strand in sc_strands:
                # do not include strands with the same name more than once
                if sc_strand.name in strand_names:
                    logger.debug('In scadnano design, found duplicate instance of strand with name '
                                 f'{sc_strand.name}; skipping all but the first when creating dsd design. '
                                 f'Please ensure that this strand really is supposed to have the same name.')
                    continue

                domain_names: List[str] = [domain.name for domain in sc_strand.domains]
                sequence = sc_strand.dna_sequence
                dsd_strand: Strand[StrandLabel, DomainLabel] = Strand(domain_names=domain_names,
                                                                      group=group,
                                                                      name=sc_strand.name,
                                                                      label=sc_strand.label)
                # assign sequence
                if sequence is not None:
                    for dsd_domain, sc_domain in zip(dsd_strand.domains, sc_strand.domains):
                        domain_sequence = sc_domain.dna_sequence()
                        # if this is a starred domain,
                        # take the WC complement first so the dsd Domain stores the "canonical" sequence
                        if sc_domain.name[-1] == '*':
                            domain_sequence = dv.wc(domain_sequence)
                        if sc.DNA_base_wildcard not in domain_sequence:
                            if fix_assigned_sequences:
                                dsd_domain.set_fixed_sequence(domain_sequence)
                            else:
                                dsd_domain.set_sequence(domain_sequence)

                # set domain labels
                for dsd_domain, sc_domain in zip(dsd_strand.domains, sc_strand.domains):
                    if dsd_domain.label is None:
                        dsd_domain.label = sc_domain.label
                    elif sc_domain.label is not None and warn_existing_domain_labels:
                        logger.warning(f'warning; dsd domain already has label {dsd_domain.label}; '
                                       f'skipping assignment of scadnano label {sc_domain.label}')

                dsd_strands.append(dsd_strand)
                strand_names.add(dsd_strand.name)

        design: Design[StrandLabel, DomainLabel] = Design(strands=dsd_strands)
        return design

    @staticmethod
    def get_group_name_from_strand_label(sc_strand: Strand) -> Any:
        if hasattr(sc_strand.label, group_key):
            return getattr(sc_strand.label, group_key)
        elif isinstance(sc_strand.label, dict) and group_key in sc_strand.label:
            return sc_strand.label[group_key]
        else:
            raise AssertionError(f'label does not have either an attribute or a dict key "{group_key}"')

    def assign_sequences_to_scadnano_design(self, sc_design: sc.Design[StrandLabel, DomainLabel],
                                            ignored_strands: Iterable[Strand] = ()) -> None:
        """
        Assigns sequences from this :any:`Design` into `sc_design`.

        Assumes that each domain name in domains in `sc_design` is a :py:data:`Domain.name` of a
        :any:`Domain` in this :any:`Design`.

        :param sc_design:
            a scadnano design
        :param ignored_strands:
            strands in the scadnano design that are to be ignored by the sequence designer
        """

        # filter out ignored strands
        sc_strands_to_include = [strand for strand in sc_design.strands if strand not in ignored_strands]

        # check types
        if not isinstance(sc_design, sc.Design):
            raise TypeError(f'sc_design must be an instance of scadnano.Design, but it is {type(sc_design)}')

        # dict mapping tuples of domain names to strands that have those domains in that order
        # sc_domain_name_tuples = {strand.domain_names_tuple(): strand for strand in self.strands}
        sc_domain_name_tuples: Dict[Tuple[str, ...], Strand] = {}
        for strand in self.strands:
            domain_names_tuple = strand.domain_names_tuple()
            sc_domain_name_tuples[domain_names_tuple] = strand

        for sc_strand in sc_strands_to_include:
            domain_names = [domain.name for domain in sc_strand.domains]
            if sc_strand.dna_sequence is None:
                assert None not in domain_names
                self._assign_to_strand_with_no_sequence(sc_strand, sc_design)
            elif None not in domain_names:
                self._assign_to_strand_with_partial_sequence(sc_strand, sc_design, sc_domain_name_tuples)
            else:
                logger.warning('Skipping assignment of DNA sequence to scadnano strand with sequence '
                               f'{sc_strand.dna_sequence}, since it has at least one domain name '
                               f'that is None.\n'
                               f'Make sure that this is a strand you intended to leave out of the '
                               f'sequence design process')

    def _assign_to_strand_with_no_sequence(self,
                                           sc_strand: sc.Strand[StrandLabel, DomainLabel],
                                           sc_design: sc.Design[StrandLabel, DomainLabel]) -> None:
        # check types
        if not isinstance(sc_design, sc.Design):
            raise TypeError(f'sc_design must be an instance of scadnano.Design, but it is {type(sc_design)}')
        if not isinstance(sc_strand, sc.Strand):
            raise TypeError(f'sc_strand must be an instance of scadnano.Strand, but it is {type(sc_strand)}')

        sequence_list: List[str] = []
        for sc_domain in sc_strand.domains:
            domain_name = sc_domain.name
            if domain_name is None:
                raise AssertionError('did not expect domain_name to be None')
            starred = domain_name[-1] == '*'
            if starred:
                domain_name = domain_name[:-1]
            dsd_domain = self.domains_by_name.get(domain_name)
            if dsd_domain is None:
                raise AssertionError(f'expected domain_name {domain_name} to be a key in domains_by_name '
                                     f'{list(self.domains_by_name.keys())}')
            domain_sequence = dsd_domain.concrete_sequence(starred)
            sequence_list.append(domain_sequence)
        strand_sequence = ''.join(sequence_list)
        sc_design.assign_dna(strand=sc_strand, sequence=strand_sequence, assign_complement=False,
                             check_length=True)

    @staticmethod
    def _assign_to_strand_with_partial_sequence(sc_strand: sc.Strand[StrandLabel, DomainLabel],
                                                sc_design: sc.Design[StrandLabel, DomainLabel],
                                                sc_domain_name_tuples: Dict[Tuple[str, ...], Strand]) -> None:

        # check types
        if not isinstance(sc_design, sc.Design):
            raise TypeError(f'sc_design must be an instance of scadnano.Design, but it is {type(sc_design)}')
        if not isinstance(sc_strand, sc.Strand):
            raise TypeError(f'sc_strand must be an instance of scadnano.Strand, but it is {type(sc_strand)}')

        # sigh: we don't have a great way to track which strand in sc_design corresponds to the same
        # strand in dsd_design (self), so we collect list of domain names in sc_strand and see if there's
        # a strand in dsd_design with the same domain names in the same order. If not we assume the strand
        # was not part of dsd_design
        domain_name_list: List[str] = []
        for sc_domain in sc_strand.domains:
            domain_name = sc_domain.name
            if domain_name is None:
                raise AssertionError('did not expect domain_name to be None')
            domain_name_list.append(domain_name)

        domain_names = tuple(domain_name_list)
        dsd_strand = sc_domain_name_tuples.get(domain_names)
        if dsd_strand is None:
            logger.warning('Skipping assignment of DNA sequence to scadnano strand with domains '
                           f'{"-".join(domain_names)}.\n'
                           f'Make sure that this is a strand you intended to leave out of the '
                           f'sequence design process')
            return

        wildcard: str = sc.DNA_base_wildcard

        sequence_list: List[str] = []
        for sc_domain, dsd_domain, domain_name in zip(sc_strand.domains, dsd_strand.domains, domain_names):
            starred = domain_name[-1] == '*'
            sc_domain_sequence = sc_domain.dna_sequence()

            # if we're in this method, then domains of sc_strand should have a partial assignment
            assert sc_domain_sequence is not None
            # now we detect whether this domain was assigned or not
            if wildcard in sc_domain_sequence:
                # if there are any '?' wildcards, then all of them should be wildcards
                assert sc_domain_sequence == wildcard * len(sc_domain_sequence)
                # if not assigned in sc_strand, we assign from dsd
                domain_sequence = dsd_domain.concrete_sequence(starred)
            else:
                # otherwise we stick with the sequence that was already assigned in sc_domain
                domain_sequence = sc_domain_sequence
                # but let's make sure dsd didn't actually change that sequence; it should have been fixed
                dsd_domain_sequence = dsd_domain.concrete_sequence(starred)
                if domain_sequence != dsd_domain_sequence:
                    raise AssertionError(f'\n    domain_sequence = {domain_sequence} is unequal to\n'
                                         f'dsd_domain_sequence = {dsd_domain_sequence}')
            sequence_list.append(domain_sequence)
        strand_sequence = ''.join(sequence_list)
        sc_design.assign_dna(strand=sc_strand, sequence=strand_sequence, assign_complement=False,
                             check_length=True)


def add_header_to_content_of_summary(report: ConstraintReport, violation_set: ViolationSet) -> str:
    score = violation_set.score_of_constraint(report.constraint)
    score_unfixed = violation_set.nonfixed_score_of_constraint(report.constraint)

    if score != score_unfixed:
        summary_score_unfixed = f'\n* unfixed score of violations: {score_unfixed:.2f}'
    else:
        summary_score_unfixed = None

    indented_content = textwrap.indent(report.content, '  ')
    delim = '*' * 80
    summary = f'''
{delim}
* {report.constraint.description}
* checks:     {report.num_checks}
* violations: {report.num_violations}
* score of violations: {score:.2f}{"" if summary_score_unfixed is None else summary_score_unfixed}
{indented_content}'''
    return summary


# represents a "Design Part", e.g., Strand, Tuple[Domain, Domain], etc... whatever portion of the Design
# is checked by the constraint
DesignPart = TypeVar('DesignPart',
                     Domain,
                     Strand,
                     Tuple[Domain, Domain],
                     Tuple[Strand, Strand],
                     Complex,
                     Iterable[Domain],
                     Iterable[Strand],
                     Iterable[Tuple[Domain, Domain]],
                     Iterable[Tuple[Strand, Strand]],
                     Iterable[Complex],
                     Design)


@dataclass
class Violation(Generic[DesignPart]):
    # Represents a violation of a single :any:`Constraint` in a :any:`Design`. The "part" of the :any:`Design`
    # that violated the constraint is generic type `DesignPart` (e.g., for :any:`StrandPairConstraint`,
    # DesignPart = :any:`Pair` [:any:`Strand`]).

    constraint: Constraint
    # :any:`Constraint` that was violated to result in this :any:`Violation`.

    domains: FrozenSet[Domain]  # = field(init=False, hash=False, compare=False, default=None)
    # :any:`Domain`'s that were involved in violating :py:data:`Violation.constraint`

    _unweighted_score: float

    def __init__(self, constraint: Constraint, domains: Iterable[Domain], score: float):
        # :param constraint:
        #     :any:`Constraint` that was violated to result in this
        # :param domains:
        #     :any:`Domain`'s that were involved in violating :py:data:`Violation.constraint`
        # :param score:
        #     total "score" of this violation, typically something like an excess energy over a
        #     threshold, squared, multiplied by the :data:`Constraint.weight`
        object.__setattr__(self, 'constraint', constraint)
        domains_frozen = frozenset(domains)
        object.__setattr__(self, 'domains', domains_frozen)
        object.__setattr__(self, '_unweighted_score', score)

    @property
    def score(self) -> float:
        return self.constraint.weight * self._unweighted_score

    def __repr__(self) -> str:
        return f'Violation({self.constraint.short_description}, score={self._unweighted_score:.2f})'

    def __str__(self) -> str:
        return repr(self)

    # _Violation equality based on identity; different Violations in memory are considered different,
    # even if all data between them matches. Don't create the same Violation twice!
    def __hash__(self):
        return super().__hash__()

    def __eq__(self, other):
        return self is other


@dataclass
class ViolationSet:
    # Represents violations of :any:`Constraint`'s in a :any:`Design`.
    #
    # It is designed to be efficiently updateable when a single :any:`Domain` changes, to efficiently update
    # only those violations of :any:`Constraint`'s that could have been affected by the changed :any:`Domain`.

    all_violations: OrderedSet[Violation] = field(default_factory=OrderedSet)
    # Set of all :any:`Violation`'s.

    domain_to_violations: Dict[Domain, OrderedSet[Violation]] = field(
        default_factory=lambda: defaultdict(OrderedSet))
    # Dict mapping each :any:`constraint.Domain` to the set of all :any:`Violation`'s for which it is blamed

    non_fixed_violations: OrderedSet[Violation] = field(default_factory=OrderedSet)

    # Set of all :any:`Violations` that are associated to non-fixed :any:`constraint.Domain`'s.

    def __repr__(self):
        lines = "\n  ".join(map(str, self.all_violations))
        return f'ViolationSet(\n  {lines})'

    def __str__(self):
        return repr(self)

    def update(self, new_violations: Dict[Domain, OrderedSet[Violation]]) -> None:
        # Update this :any:`ViolationSet` by merging in new violations from `new_violations`.
        #
        # :param new_violations: dict mapping each :any:`Domain` to the set of :any:`Violation`'s
        #                        for which it is blamed
        for domain, domain_violations in new_violations.items():
            self.all_violations.update(domain_violations)
            self.domain_to_violations[domain].update(domain_violations)
            if not domain.fixed:
                self.non_fixed_violations.update(domain_violations)

    def clone(self) -> ViolationSet:
        # Returns a deep-ish copy of this :any:`ViolationSet`.
        # :py:data:`ViolationSet.all_violations` is a new list,
        # but containing the same :any:`Violation`'s.
        # :py:data:`ViolationSet.domain_to_violations` is a new dict,
        # and each of its values is a new set, but each of the :any:`Domain`'s and :any:`Violation`'s
        # is the same object as in the original :any:`ViolationSet`.
        #
        # This is required for efficiently processing :any:`Violation`'s from one search iteration to the next.
        #
        # :return: A deep-ish copy of this :any:`ViolationSet`.
        domain_to_violations_deep_copy = defaultdict(OrderedSet, self.domain_to_violations)
        for domain, violations in domain_to_violations_deep_copy.items():
            domain_to_violations_deep_copy[domain] = OrderedSet(violations)
        return ViolationSet(OrderedSet(self.all_violations), domain_to_violations_deep_copy,
                            OrderedSet(self.non_fixed_violations))

    def remove_violations_of_domain(self, domain: Domain) -> None:
        # Removes any :any:`Violation`'s blamed on `domain`.
        # :param domain: the :any:`Domain` whose :any:`Violation`'s should be removed

        # XXX: need to make a copy of this set, since we are modifying the sets in place
        # (values in self.domain_to_violations)
        violations_of_domain = set(self.domain_to_violations[domain])
        self.all_violations -= violations_of_domain
        self.non_fixed_violations -= violations_of_domain
        for violations_of_other_domain in self.domain_to_violations.values():
            violations_of_other_domain -= violations_of_domain
        assert len(self.domain_to_violations[domain]) == 0

    def total_score(self) -> float:
        """
        :return: Total score of all violations.
        """
        return sum(violation.score for violation in self.all_violations)

    def total_nonfixed_score(self) -> float:
        # :return:
        #     Total score of all violations attributed to :any:`constraint.Domain`'s with
        #     :any:`constraint.Domain.fixed` = False.
        return sum(violation.score for violation in self.non_fixed_violations)

    def score_of_constraint(self, constraint: Constraint) -> float:
        """
        :param constraint:
            constraint to filter scores on
        :return:
            Total score of all violations due to `constraint`.
        """
        return sum(violation.score for violation in self.all_violations if violation.constraint == constraint)

    def nonfixed_score_of_constraint(self, constraint: Constraint) -> float:
        # :param constraint:
        #     constraint to filter scores on
        # :return:
        #     Total score of all nonfixed violations due to `constraint`.
        return sum(
            violation.score for violation in self.non_fixed_violations if violation.constraint == constraint)

    def num_violations(self) -> float:
        # :return: Total number of violations.
        return len(self.all_violations)

    def num_nonfixed_violations(self) -> float:
        # :return: Total number of nonfixed violations.
        return len(self.non_fixed_violations)


@dataclass(frozen=True, eq=False)
class Constraint(ABC, Generic[DesignPart]):
    description: str
    """Description of the constraint, e.g., 'strand has secondary structure exceeding -2.0 kcal/mol'."""

    short_description: str = ''
    """
    Very short description of the constraint suitable for compactly logging to the screen, e.g., 'strand_ss'
    """

    weight: float = 1.0
    """
    Constant multiplier Weight of the problem; the higher the total weight of all the :any:`Constraint`'s 
    a :any:`Domain` has caused, the greater likelihood its sequence is changed when stochastically searching 
    for sequences to satisfy all constraints.
    """

    score_transfer_function: Callable[[float], float] = default_score_transfer_function
    """
    Score transfer function to use. When a constraint is violated, the constraint returns a nonnegative
    float (the score) indicating the "severity" of the violation. For example, if a :any:`Strand` has 
    secondary structure energy exceeding a threshold, it will return the difference between the energy and 
    the threshold.
    It is then passed through the `score_transfer_function`.
    The default is the squared ReLU function: f(x) = max(0, x^2).
    This "punishes" more severe violations more, i.e., it would
    bring down the total score of violations more to reduce a violation 3 kcal/mol in excess of its
    threshold than to reduce (by the same amount) a violation only 1 kcal/mol in excess of its threshold.
    """

    threaded: bool = False
    """
    Whether or not to use parallelization across multiple processes to take advantage of multiple
    processors/cores. (Not applicable to some types of constraints. 
    """

    sequence_only: bool = True
    """
    If :py:data:`Constraint.threaded` is True, then this should be set to True so that only the 
    sequence is serialized when passing data to other processes for parallel computation.
    Otherwise, significant time is spent serializing objects such as :any:`Strand` or :any:`Domain`,
    which is slower than not using parallelization in the first place.
    """

    _evaluate: Callable[[Tuple[str, ...], Optional[DesignPart]],
                        Tuple[float, str]] = lambda _: (0.0, 'no summary')

    def __post_init__(self) -> None:
        if len(self.short_description) == 0:
            # self.short_description = self.description
            object.__setattr__(self, 'short_description', self.description)

        if self.weight <= 0:
            raise ValueError(f'weight must be positive but it is {self.weight}')

        if self.threaded and not self.sequence_only:
            raise ValueError('cannot have both threaded=True and sequence_only=False;\n'
                             'if you want to use threading, the constraint must be sequence_only '
                             'for efficiency, since the pathos library is used for parallel processing, '
                             'and it is too inefficient to serialize anything more than the DNA sequence')

    def evaluate(self, seqs: Tuple[str, ...], part: Optional[DesignPart]) -> Tuple[float, str]:
        """
        Evaluates this :any:`Constraint` using function `evaluate` supplied in constructor.

        :param seqs:
            sequence(s) of relevant :any:`DesignPart`, e.g., if `part` is a pair of :any:`Strand`'s,
            then `seqs` is a pair of strings
        :param part:
            the :any:`DesignPart` to be evaluated. Might be None if parallelization is being used,
            since it is cheaper to serialize only the sequence(s) than the entire :any:`DesignPart`
            for passing to other processes to evaluate in parallel.
        :return:
            pair (`excess`, `summary`), where `excess` is a float indicating how much the constraint
            was violated (0.0 if satisfied) and `summary` is a string summarizing the violation (or lack
            thereof), suitable for printing into a line of a report. For example, if measuring
            complex free energy -2.5 kcal/mol of a strand and comparing against a threshold -1.0 kcal/mol,
            `excess` might be the difference 1.5 between the energy and the threshold,
            and `summary` might be the string "-2.5 kcal/mol".
        """
        self._check_sequence_only(part)
        # excess = summary = None
        # try:
        excess, summary = (self._evaluate)(seqs, part)  # noqa
        # except ValueError as err:
        #     msg = str(err)
        #     if 'too many values to unpack' in msg or 'not enough values to unpack' in msg:
        #         regex = re.compile(r'.*expected (\d+).*')
        #         match = regex.match(msg)
        #         num_expected_str = match.group(1)
        #         num_expected = int(num_expected_str)
        #         raise AssertionError(f'error: should have passed {num_expected} sequences to evaluate\n'
        #                              f'but instead passed {len(seqs)}.\n'
        #                              f'This is for constraint with description\n{self.description}')
        # assert excess is not None and summary is not None
        if excess < 0.0:
            excess = 0.0
        score = self.score_transfer_function(excess)
        return score, summary

    def _check_sequence_only(self, part: DesignPart) -> None:
        if not self.sequence_only and part is None:
            raise AssertionError('if sequence_only is False, '
                                 'then the design part cannot be None, but it is')

    # @abstractmethod
    # def generate_summary(self, design_part: DesignPart, report_only_violations: bool) -> ConstraintReport:
    #     """
    #     Method that helps to give a summary of how well parts of the :any:`Design` are performing in
    #     satisfying this constraint. For example, useful for generating a report after assigning sequences
    #     to a :any:`Design`.
    #
    #     :param design_part:
    #         part of :any:`Design` that this :any:`Constraint` references
    #     :param report_only_violations:
    #         Whether to report only violations of constraints, or all evaluations of constraints, including
    #         those that passed.
    #     :return:
    #         :any:`ConstraintReport` summarizing of how well `design_part` "performs" for :any:`Constraint`.
    #         For example, a :any:`StrandConstraint` checking the complex free energy of a single
    #         :any:`Strand` may return a string such as "strand complex free energy: -2.3 kcal/mol".
    #         A :any:`StrandPairsConstraint` checking all pairs of :any:`Strand`'s may return a longer
    #         string reporting on every pair.
    #     """
    #     raise NotImplementedError('subclasses of Constraint must implement generate_summary')


_no_summary_string = f"No summary for this constraint. " \
                     f"To generate one, pass a function as the parameter named " \
                     f'"summary" when creating the Constraint.'


@dataclass(frozen=True, eq=False)
class ConstraintWithDomains(Constraint[DesignPart], Generic[DesignPart]):
    domains: Optional[Tuple[Domain, ...]] = None
    """
    Tuple of :any:`Domain`'s to check; if not specified, all :any:`Domain`'s in :any:`Design` are checked.
    """


@dataclass(frozen=True, eq=False)
class ConstraintWithStrands(Constraint[DesignPart], Generic[DesignPart]):
    strands: Optional[Tuple[Strand, ...]] = None
    """
    Tuple of :any:`Strand`'s to check; if not specified, all :any:`Strand`'s in :any:`Design` are checked.
    """


@dataclass(frozen=True, eq=False)  # type: ignore
class DomainConstraint(ConstraintWithDomains[Domain]):
    """Constraint that applies to a single :any:`Domain`."""


@dataclass(frozen=True, eq=False)  # type: ignore
class StrandConstraint(ConstraintWithStrands[Strand]):
    """Constraint that applies to a single :any:`Strand`."""


@dataclass(frozen=True, eq=False)
class ConstraintWithDomainPairs(Constraint[DesignPart], Generic[DesignPart]):
    pairs: Optional[Tuple[Tuple[Domain, Domain], ...]] = None
    """
    List of pairs of :any:`Domain`'s to check; if not specified, all pairs in :any:`Design` are checked.
    """


@dataclass(frozen=True, eq=False)
class ConstraintWithStrandPairs(Constraint[DesignPart], Generic[DesignPart]):
    pairs: Optional[Tuple[Tuple[Strand, Strand], ...]] = None
    """
    List of pairs of :any:`Strand`'s to check; if not specified, all pairs in :any:`Design` are checked.
    """


@dataclass(frozen=True, eq=False)  # type: ignore
class DomainPairConstraint(ConstraintWithDomainPairs[Tuple[Domain, Domain]]):
    """Constraint that applies to a pair of :any:`Domain`'s."""


@dataclass(frozen=True, eq=False)  # type: ignore
class StrandPairConstraint(ConstraintWithStrandPairs[Tuple[Strand, Strand]]):
    """Constraint that applies to a pair of :any:`Strand`'s."""


@dataclass(frozen=True, eq=False)  # type: ignore
class DomainPairsConstraint(ConstraintWithDomainPairs[Iterable[Tuple[Domain, Domain]]]):
    """
    Similar to :any:`DomainsConstraint` but operates on a specified list of pairs of :any:`Domain`'s.
    """

    evaluate: Callable[[Iterable[Tuple[Domain, Domain]]],
                       List[Tuple[OrderedSet[Domain], float]]] = lambda _: []
    """
    Pairwise check to perform on :any:`Domain`'s.
    Returns True if and only if the all pairs in the input iterable satisfy the constraint.
    """

    summary: Callable[[Iterable[Tuple[Domain, Domain]], bool],
                      ConstraintReport] = lambda _: _no_summary_string

    def __call__(self, domain_pairs: Iterable[Tuple[Domain, Domain]]) \
            -> List[Tuple[OrderedSet[Domain], float]]:
        sets_excesses = (self.evaluate)(domain_pairs)  # noqa
        sets_scores = _alter_scores_by_transfer(sets_excesses, self.score_transfer_function)
        return sets_scores

    def generate_summary(self, domain_pairs: Iterable[Tuple[Domain, Domain]],
                         report_only_violations: bool) -> ConstraintReport:
        return (self.summary)(domain_pairs, report_only_violations)  # noqa


@dataclass(frozen=True, eq=False)  # type: ignore
class StrandPairsConstraint(ConstraintWithStrandPairs[Iterable[Tuple[Strand, Strand]]]):
    """
    Similar to :any:`StrandsConstraint` but operates on a specified list of pairs of :any:`Strand`'s.
    """

    evaluate: Callable[[Iterable[Tuple[Strand, Strand]]],
                       List[Tuple[OrderedSet[Domain], float]]] = lambda _: []
    """
    Pairwise check to perform on :any:`Strand`'s.
    Returns True if and only if the all pairs in the input iterable satisfy the constraint.
    """

    summary: Callable[[Iterable[Tuple[Strand, Strand]], bool],
                      ConstraintReport] = lambda _: _no_summary_string

    def __call__(self, strand_pairs: Iterable[Tuple[Strand, Strand]]) \
            -> List[Tuple[OrderedSet[Domain], float]]:
        sets_excesses = (self.evaluate)(strand_pairs)  # noqa
        sets_scores = _alter_scores_by_transfer(sets_excesses, self.score_transfer_function)
        return sets_scores

    def generate_summary(self, strand_pairs: Iterable[Tuple[Strand, Strand]],
                         report_only_violations: bool) -> ConstraintReport:
        return (self.summary)(strand_pairs, report_only_violations)  # noqa


@dataclass(frozen=True, eq=False)  # type: ignore
class DomainsConstraint(ConstraintWithDomains[Iterable[Domain]]):
    """
    Constraint that applies to a several :any:`Domain`'s. The difference with :any:`DomainConstraint` is that
    the caller may want to process all :any:`Domain`'s at once, e.g., by giving many of them to a third-party
    program such as ViennaRNA, which may be more efficient than repeatedly calling a Python function.

    It *is* assumed that the constraint works by checking one :any:`Domain` at a time. After computing
    initial violations of constraints, subsequent calls to this constraint only give the domain that was
    mutated, not the entire of :any:`Domain`'s in the whole :any:`Design`.
    Use :any:`DesignConstraint` for constraints that require every :any:`Domain` in the :any:`Design`.

    Return value is a list of sets of :any:`Domain`'s. Each element of the list corresponds to one violation
    of the :any:`DomainsConstraint`. The search will assign to a :any:`Domain` `d` a weight of
    :py:data:`Constraint.weight` once for each set in this list that contains `d`. For example, if
    :py:data:`Constraint.weight` is 1.0, the the return value is ``[{d1, d2}, {d2, d3}]``, then
    ``d1`` and ``d3`` are assigned weight 1.0, and ``d2`` is assigned weight 2.0.
    """

    evaluate: Callable[[Iterable[Domain]],
                       List[Tuple[OrderedSet[Domain], float]]] = lambda _: []

    summary: Callable[[Iterable[Domain], bool],
                      ConstraintReport] = lambda _: _no_summary_string

    def __call__(self, domains: Iterable[Domain]) -> List[Tuple[OrderedSet[Domain], float]]:
        sets_excesses = (self.evaluate)(domains)  # noqa
        sets_scores = _alter_scores_by_transfer(sets_excesses, self.score_transfer_function)
        return sets_scores

    def generate_summary(self, domains: Iterable[Domain], report_only_violations: bool) -> ConstraintReport:
        return (self.summary)(domains, report_only_violations)  # noqa


@dataclass(frozen=True, eq=False)  # type: ignore
class StrandsConstraint(ConstraintWithStrands[Iterable[Strand]]):
    """
    Constraint that applies to a several :any:`Strand`'s. The difference with :any:`StrandConstraint` is that
    the caller may want to process all :any:`Strand`'s at once, e.g., by giving many of them to a third-party
    program such as ViennaRNA.

    It *is* assumed that the constraint works by checking one :any:`Strand` at a time. After computing
    initial violations of constraints, subsequent calls to this constraint only give strands containing
    the domain that was mutated, not the entire of :any:`Strand`'s in the whole :any:`Design`.
    Use :any:`DesignConstraint` for constraints that require every :any:`Strand` in the :any:`Design`.

    Return value is a list of sets of :any:`Domain`'s. Each element of the list corresponds to one violation
    of the :any:`StrandsConstraint`. The search will assign to a :any:`Domain` `d` a weight of
    :py:data:`Constraint.weight` once for each set in this list that contains `d`. For example, if
    :py:data:`Constraint.weight` is 1.0, the the return value is ``[{d1, d2}, {d2, d3}]``, then
    ``d1`` and ``d3`` are assigned weight 1.0, and ``d2`` is assigned weight 2.0.
    """

    evaluate: Callable[[Iterable[Strand]],
                       List[Tuple[OrderedSet[Domain], float]]] = lambda _: []

    summary: Callable[[Iterable[Strand], bool],
                      ConstraintReport] = lambda _: _no_summary_string

    def __call__(self, strands: Iterable[Strand]) -> List[Tuple[OrderedSet[Domain], float]]:
        sets_excesses = (self.evaluate)(strands)  # noqa
        sets_scores = _alter_scores_by_transfer(sets_excesses, self.score_transfer_function)
        return sets_scores

    def generate_summary(self, strands: Iterable[Strand], report_only_violations: bool) -> ConstraintReport:
        return (self.summary)(strands, report_only_violations)  # noqa


@dataclass(frozen=True, eq=False)  # type: ignore
class DesignConstraint(Constraint[Design]):
    """
    Constraint that applies to the entire :any:`Design`. This is used for any :any:`Constraint` that
    does not naturally fit the structure of the other types of constraints.

    There is an optional parameter `domain_changed`, defaulting to None; if specified, the constraint
    can restrict its check on the assumption that only `domain_changed` has changed since the last
    time the violations were collected. In other words, it need only return sets of :any:`Domain`'s involving
    violations of this :any:`DesignConstraint` that involve `domain_changed`. For example, if it checks
    all triples of :any:`Domain`'s, there is no need to check any triple not containing `domain_changed`.

    Return value is a list of sets of :any:`Domain`'s. Each element of the list corresponds to one violation
    of the :any:`DomainsConstraint`. The search will assign to a :any:`Domain` `d` a weight of
    :py:data:`Constraint.weight` once for each set in this list that contains `d`. For example, if
    :py:data:`Constraint.weight` is 1.0, the the return value is ``[{d1, d2}, {d2, d3}]``, then
    ``d1`` and ``d3`` are assigned weight 1.0, and ``d2`` is assigned weight 2.0.
    """

    evaluate: Callable[[Design, Optional[Domain]],
                       List[Tuple[OrderedSet[Domain], float]]] = lambda _, __: []

    summary: Callable[[Design, bool],
                      ConstraintReport] = lambda _: _no_summary_string

    def __call__(self, design: Design, domains_changed: Optional[Iterable[Domain]]) \
            -> List[Tuple[OrderedSet[Domain], float]]:
        sets_excesses = (self.evaluate)(design, domains_changed)  # noqa
        sets_scores = _alter_scores_by_transfer(sets_excesses, self.score_transfer_function)
        return sets_scores

    def generate_summary(self, design: Design, report_only_violations: bool) -> ConstraintReport:
        return (self.summary)(design, report_only_violations)  # noqa


def verify_designs_match(design1: Design, design2: Design, check_fixed: bool = True) -> None:
    """
    Verifies that two designs match, other than their constraints. This is useful when loading a
    design that has been saved in the middle of searching for DNA sequences, to verify that it matches
    a design created before the DNA sequence search started.

    :param design1:
        A :any:`Design`.
    :param design2:
        Another :any:`Design`.
    :param check_fixed:
        Whether to check for fixed sequences equal between the two (may want to not check in case these
        are set later).
    :raises ValueError:
        If the designs do not match.
        Here is what is checked:
        - strand names and group names appear in the same order
        - domain names and pool names appear in the same order in strands with the same name
        - :py:data:`Domain.fixed` matches between :any:`Domain`'s
    """
    for idx, (strand1, strand2) in enumerate(zip(design1.strands, design2.strands)):
        if strand1.name != strand2.name:
            raise ValueError(f'strand names at position {idx} don\'t match: '
                             f'{strand1.name} and {strand2.name}')
        if (strand1.group is not None
                and strand2.group is not None
                and strand1.group.name != strand2.group.name):  # noqa
            raise ValueError(f'strand {strand2.name} group name does not match:'
                             f'design1 strand {strand1.name} group = {strand1.group},\n'
                             f'design2 strand {strand2.name} group = {strand2.group}')
        for domain1, domain2 in zip(strand1.domains, strand2.domains):
            if domain1.name != domain2.name:
                raise ValueError(f'domain of strand {strand2.name} don\'t match: '
                                 f'{strand1.domains} and {strand2.domains}')
            if check_fixed and domain1.fixed != domain2.fixed:
                raise ValueError(f'domain {domain2.name} is fixed in one but not the other:\n'
                                 f'design1 domain {domain1.name} fixed = {domain1.fixed},\n'
                                 f'design2 domain {domain2.name} fixed = {domain2.fixed}')
            if (domain1.has_pool()
                    and domain2.has_pool()
                    and domain1.pool.name != domain2.pool.name):
                raise ValueError(f'domain {domain2.name} pool name does not match:'
                                 f'design1 domain {domain1.name} pool = {domain1.pool.name},\n'
                                 f'design2 domain {domain2.name} pool = {domain2.pool.name}')


def convert_threshold(threshold: Union[float, Dict[T, float]], key: T) -> float:
    """
    :param threshold: either a single float, or a dictionary mapping instances of T to floats
    :param key: instance of T
    :return: threshold for key
    """
    threshold_value: float
    if isinstance(threshold, float):
        threshold_value = threshold
    elif isinstance(threshold, dict):
        threshold_value = threshold[key]
    else:
        raise ValueError(f'threshold = {threshold} must be one of float or dict, '
                         f'but it is {type(threshold)}')
    return threshold_value


def nupack_domain_complex_free_energy_constraint(
        threshold: float,
        temperature: float = dv.default_temperature,
        sodium: float = dv.default_sodium,
        magnesium: float = dv.default_magnesium,
        weight: float = 1.0,
        score_transfer_function: Callable[[float], float] = default_score_transfer_function,
        threaded: bool = False,
        description: Optional[str] = None,
        short_description: str = 'strand_ss_nupack',
        domains: Optional[Iterable[Domain]] = None) -> DomainConstraint:
    """
    Returns constraint that checks individual :any:`Domain`'s for excessive interaction using
    NUPACK's pfunc.

    NUPACK 4 must be installed. Installation instructions can be found at
    https://piercelab-caltech.github.io/nupack-docs/start/.

    :param threshold:
        energy threshold in kcal/mol
    :param temperature:
        temperature in Celsius
    :param sodium:
        molarity of sodium (more generally, monovalent ions such as Na+, K+, NH4+)
        in moles per liter
    :param magnesium:
        molarity of magnesium (Mg++) in moles per liter
    :param weight:
        how much to weigh this :any:`Constraint`
    :param score_transfer_function:
        See :py:data:`Constraint.score_transfer_function`.
    :param threaded:
        Whether to use threadds to parallelize.
    :param domains:
        :any:`Domain`'s to check; if not specified, all domains are checked.
    :param description:
        detailed description of constraint suitable for putting in report; if not specified
        a reasonable default is chosen
    :param short_description:
        short description of constraint suitable for logging to stdout
    :return:
        the constraint
    """

    def evaluate(seqs: Tuple[str], domain: Optional[Domain]) -> Tuple[float, str]:
        sequence = seqs[0]
        energy = dv.complex_free_energy_single_strand(sequence, temperature, sodium, magnesium)
        if domain is not None:
            logger.debug(
                f'domain ss threshold: {threshold:6.2f} '
                f'secondary_structure_single_strand({domain.name, temperature}) = {energy:6.2f} ')
        excess = threshold - energy
        return max(0.0, excess), f'{energy:6.2f} kcal/mol'

    if description is None:
        description = f'NUPACK secondary structure of domain exceeds {threshold} kcal/mol'

    if domains is not None:
        domains = tuple(domains)

    return DomainConstraint(description=description,
                            short_description=short_description,
                            weight=weight,
                            score_transfer_function=score_transfer_function,
                            _evaluate=evaluate,
                            threaded=threaded,
                            domains=domains)


def nupack_strand_complex_free_energy_constraint(
        threshold: float,
        temperature: float = dv.default_temperature,
        sodium: float = dv.default_sodium,
        magnesium: float = dv.default_magnesium,
        weight: float = 1.0,
        score_transfer_function: Callable[[float], float] = default_score_transfer_function,
        threaded: bool = False,
        description: Optional[str] = None,
        short_description: str = 'strand_ss_nupack',
        strands: Optional[Iterable[Strand]] = None) -> StrandConstraint:
    """
    Returns constraint that checks individual :any:`Strand`'s for excessive interaction using
    NUPACK's pfunc.

    NUPACK 4 must be installed. Installation instructions can be found at
    https://piercelab-caltech.github.io/nupack-docs/start/.

    :param threshold:
        energy threshold in kcal/mol
    :param temperature:
        temperature in Celsius
    :param sodium:
        molarity of sodium (more generally, monovalent ions such as Na+, K+, NH4+)
        in moles per liter
    :param magnesium:
        molarity of magnesium (Mg++) in moles per liter
    :param weight:
        how much to weigh this :any:`Constraint`
    :param score_transfer_function:
        See :py:data:`Constraint.score_transfer_function`.
    :param threaded:
        Whether to use threadds to parallelize.
    :param strands:
        Strands to check; if not specified, all strands are checked.
    :param description:
        detailed description of constraint suitable for putting in report; if not specified
        a reasonable default is chosen
    :param short_description:
        short description of constraint suitable for logging to stdout
    :return:
        the constraint
    """

    def evaluate(seqs: Tuple[str], strand: Optional[Strand]) -> Tuple[float, str]:
        sequence = seqs[0]
        energy = dv.complex_free_energy_single_strand(sequence, temperature, sodium, magnesium)
        if strand is not None:
            logger.debug(
                f'strand ss threshold: {threshold:6.2f} '
                f'secondary_structure_single_strand({strand.name, temperature}) = {energy:6.2f} ')
        excess = threshold - energy
        return max(0.0, excess), f'{energy:6.2f} kcal/mol'

    # def summary(strand: Strand) -> str:
    #     sequence = strand.sequence()
    #     energy = dv.complex_free_energy_single_strand(sequence, temperature, sodium, magnesium)
    #     return f'{energy:6.2f} kcal/mol'

    if description is None:
        description = f'NUPACK secondary structure of strand exceeds {threshold} kcal/mol'

    if strands is not None:
        strands = tuple(strands)

    return StrandConstraint(description=description,
                            short_description=short_description,
                            weight=weight,
                            score_transfer_function=score_transfer_function,
                            _evaluate=evaluate,
                            threaded=threaded,
                            strands=strands)


def nupack_domain_pair_constraint(
        threshold: float,
        temperature: float = dv.default_temperature,
        threaded: bool = False,
        weight: float = 1.0,
        score_transfer_function: Callable[[float], float] = default_score_transfer_function,
        description: Optional[str] = None,
        short_description: str = 'dom_pair_nupack',
        pairs: Optional[Iterable[Tuple[Domain, Domain]]] = None,
) -> DomainPairConstraint:
    """
    Returns constraint that checks given pairs of :any:`Domain`'s for excessive interaction using
    NUPACK's pfunc executable. Each of the four combinations of seq1, seq2 and their Watson-Crick complements
    are compared.

    :param threshold:
        Energy threshold in kcal/mol.
    :param temperature:
        Temperature in Celsius
    :param threaded:
        Whether to test the each pair of :any:`Domain`'s in parallel (i.e., sets field
        :py:data:`Constraint.threaded`)
    :param weight:
        How much to weigh this :any:`Constraint`.
    :param score_transfer_function:
        See :py:data:`Constraint.score_transfer_function`.
    :param description:
        Detailed description of constraint suitable for summary report.
    :param short_description:
        Short description of constraint suitable for logging to stdout.
    :param pairs:
        Pairs of :any:`Domain`'s to compare; if not specified, checks all pairs (including a
        :any:`Domain` against itself).
    :return:
        The :any:`DomainPairConstraint`.
    """

    if description is None:
        if isinstance(threshold, Number):
            description = f'NUPACK energy of domain pair exceeds {threshold} kcal/mol'
        elif isinstance(threshold, dict):
            domain_pool_name_pair_to_threshold = {(domain_pool1.name, domain_pool2.name): value
                                                  for (domain_pool1, domain_pool2), value in
                                                  threshold.items()}
            description = f'NUPACK energy of domain pair exceeds threshold defined by their DomainPools ' \
                          f'as follows:\n{domain_pool_name_pair_to_threshold}'
        else:
            raise ValueError(f'threshold = {threshold} must be one of float or dict, '
                             f'but it is {type(threshold)}')

    def binding_closure(seq_pair: Tuple[str, str]) -> float:
        return dv.binding(seq_pair[0], seq_pair[1], temperature=temperature)

    # def evaluate(seq1: str, seq2: str, domain1: Optional[Domain], domain2: Optional[Domain]) -> float:
    def evaluate(seqs: Tuple[str, ...], domains: Optional[Tuple[Domain, Domain]]) -> Tuple[float, str]:
        seq1, seq2 = seqs
        domain1, domain2 = domains if domains is not None else (None, None)
        name_pairs = [(None, None)] * 4
        if domain1 is not None and domain2 is not None:
            seq_pairs, name_pairs, _ = _all_pairs_domain_sequences_complements_names_from_domains(
                [(domain1, domain2)])
        else:
            # If seq1==seq2, don't check d-d* or d*-d in this case, but do check d-d and d*-d*
            seq_pairs = [
                (seq1, seq2),
                (dv.wc(seq1), dv.wc(seq2)),
            ]
            if seq1 != seq2:
                # only check these if domains are not the same
                seq_pairs.extend([
                    (seq1, dv.wc(seq2)),
                    (dv.wc(seq1), seq2),
                ])

        energies: List[float]
        energies = []
        for seq_pair in seq_pairs:
            energy = binding_closure(seq_pair)
            energies.append(energy)

        excesses: List[float] = []
        for energy, (name1, name2) in zip(energies, name_pairs):
            if name1 is not None and name2 is not None:
                logger.debug(
                    f'domain pair threshold: {threshold:6.2f} '
                    f'binding({name1}, {name2}, {temperature}) = {energy:6.2f} ')
            excess = threshold - energy
            excesses.append(excess)

        max_excess = max(excesses)

        max_name_length = max(len(name) for name in _flatten(name_pairs))
        lines = [f'{name1:{max_name_length}}, '
                 f'{name2:{max_name_length}}: '
                 f' {energy:6.2f} kcal/mol'
                 for (name1, name2), energy in zip(name_pairs, energies)]
        msg = '\n  ' + '\n  '.join(lines)

        return max(0.0, max_excess), msg

    # def summary(domain1: Domain, domain2: Domain) -> str:
    #     seq_pairs, domain_name_pairs, _ = _all_pairs_domain_sequences_complements_names_from_domains(
    #         [(domain1, domain2)])
    #     energies = []
    #     for seq1, seq2 in seq_pairs:
    #         energy = dv.binding(seq1, seq2, temperature=temperature)
    #         energies.append(energy)
    #     max_name_length = max(len(name) for name in _flatten(domain_name_pairs))
    #     lines = [f'{name1:{max_name_length}}, '
    #              f'{name2:{max_name_length}}: '
    #              f' {energy:6.2f} kcal/mol'
    #              for (name1, name2), energy in zip(domain_name_pairs, energies)]
    #     return '\n  ' + '\n  '.join(lines)

    if pairs is not None:
        pairs = tuple(pairs)

    return DomainPairConstraint(description=description,
                                short_description=short_description,
                                weight=weight,
                                score_transfer_function=score_transfer_function,
                                _evaluate=evaluate,
                                # summary=summary,
                                threaded=threaded,
                                pairs=pairs)


def nupack_strand_pair_constraint(
        threshold: float,
        temperature: float = dv.default_temperature,
        sodium: float = dv.default_sodium,
        magnesium: float = dv.default_magnesium,
        weight: float = 1.0,
        score_transfer_function: Callable[[float], float] = default_score_transfer_function,
        description: Optional[str] = None,
        short_description: str = 'strand_pair_nupack',
        threaded: bool = False,
        pairs: Optional[Iterable[Tuple[Strand, Strand]]] = None,
) -> StrandPairConstraint:
    """
    Returns constraint that checks given pairs of :any:`Strand`'s for excessive interaction using
    NUPACK's pfunc executable.

    NUPACK 4 must be installed. Installation instructions can be found at
    https://piercelab-caltech.github.io/nupack-docs/start/.

    :param threshold:
        Energy threshold in kcal/mol
    :param temperature:
        Temperature in Celsius
    :param sodium:
        concentration of Na+ in molar
    :param magnesium:
        concentration of Mg++ in molar
    :param weight:
        How much to weigh this :any:`Constraint`.
    :param score_transfer_function:
        See :py:data:`Constraint.score_transfer_function`.
    :param threaded:
        Whether to use threading to parallelize evaluating this constraint.
    :param description:
        Detailed description of constraint suitable for report.
    :param short_description:
        Short description of constraint suitable for logging to stdout.
    :param pairs:
        Pairs of :any:`Strand`'s to compare; if not specified, checks all pairs (including a
        :any:`Strand` against itself).
    :return:
        The :any:`StrandPairConstraint`.
    """

    if description is None:
        description = f'NUPACK binding energy of strand pair exceeds {threshold} kcal/mol'

    # def evaluate(sequence1: str, sequence2: str,
    #              strand1: Optional[Strand], strand2: Optional[Strand]) -> float:
    def evaluate(seqs: Tuple[str, ...],
                 strands: Optional[Tuple[Strand, Strand]]) -> Tuple[float, str]:
        sequence1, sequence2 = seqs
        strand1, strand2 = strands if strands is not None else (None, None)
        energy = dv.binding(sequence1, sequence2, temperature=temperature,
                            sodium=sodium, magnesium=magnesium)
        if strand1 is not None and strand2 is not None:
            logger.debug(
                f'strand pair threshold: {threshold:6.2f} '
                f'binding({strand1.name, strand2.name, temperature}) = {energy:6.2f} ')
        excess = threshold - energy
        return max(0.0, excess), f'{energy:6.2f} kcal/mol'

    # def summary(strand1: Strand, strand2: Strand) -> str:
    #     energy = dv.binding(strand1.sequence(), strand2.sequence(), temperature=temperature,
    #                         sodium=sodium, magnesium=magnesium)
    #     return f'{energy:6.2f} kcal/mol'

    if pairs is not None:
        pairs = tuple(pairs)

    return StrandPairConstraint(description=description,
                                short_description=short_description,
                                weight=weight,
                                score_transfer_function=score_transfer_function,
                                threaded=threaded,
                                pairs=pairs,
                                _evaluate=evaluate,
                                # summary=summary
                                )


def chunker(sequence: Sequence[T], chunk_length: Optional[int] = None, num_chunks: Optional[int] = None) -> \
        List[List[T]]:
    """
    Collect data into fixed-length chunks or blocks, e.g., chunker('ABCDEFG', 3) --> ABC DEF G

    :param sequence:
        Sequence (list or tuple) of items.
    :param chunk_length:
        Length of each chunk. Mutually exclusive with `num_chunks`.
    :param num_chunks:
        Number of chunks. Mutually exclusive with `chunk_length`.
    :return:
        List of `num_chunks` lists, each list of length `chunk_length` (one of `num_chunks` or
        `chunk_length` will be calculated from the other).
    """
    if chunk_length is None and num_chunks is None or chunk_length is not None and num_chunks is not None:
        raise ValueError('exactly one of chunk_length or num_chunks must be None')

    if chunk_length is None:
        if num_chunks is None:
            raise ValueError('exactly one of chunk_length or num_chunks must be None')
        if num_chunks < 1:
            raise ValueError('num_chunks must be positive')
        num_items = len(sequence)
        chunk_length, remainder = divmod(num_items, num_chunks)
        if remainder > 0:
            chunk_length += 1

    args = [iter(sequence)] * chunk_length
    chunks = list(itertools.zip_longest(*args, fillvalue=None))
    for i, chunk in enumerate(chunks):
        chunks[i] = [item for item in chunks[i] if item is not None]
    return chunks


def cpu_count(logical: bool = False) -> int:
    """
    Counts the number of physical CPUs (cores). For greatest accuracy, requires the 3rd party
    `psutil <https://pypi.org/project/psutil/>`_
    package to be installed.

    :param logical:
        Whether to count number of logical processors or physical CPU cores.
    :return:
        Number of physical CPU cores if logical is False and package psutils is installed;
        otherwise, the number of logical processors.
    """
    count: Optional[int]
    try:
        import psutil  # type: ignore
        count = psutil.cpu_count(logical=logical)
    except ModuleNotFoundError:
        logger.warning('''\
psutil package not installed. Using os package to determine number of cores.
WARNING: this will count the number of logical cores, but the number of
physical cores is a more effective number to use. It is recommended to
install the package psutil to help determine the number of physical cores
and make parallel processing more efficient:
  https://pypi.org/project/psutil/''')
        count = os.cpu_count()
    if count is None:
        logger.warning('could not determine number of physical CPU cores; defaulting to 1')
        count = 1
    return count


def rna_duplex_strand_pairs_constraint(
        threshold: float,
        temperature: float = dv.default_temperature,
        weight: float = 1.0,
        score_transfer_function: Callable[[float], float] = default_score_transfer_function,
        description: Optional[str] = None,
        short_description: str = 'rna_dup_strand_pairs',
        threaded: bool = False,
        pairs: Optional[Iterable[Tuple[Strand, Strand]]] = None,
        parameters_filename: str = dv.default_vienna_rna_parameter_filename) \
        -> StrandPairsConstraint:
    """
    Returns constraint that checks given pairs of :any:`Strand`'s for excessive interaction using
    Vienna RNA's RNAduplex executable.

    :param threshold:
        Energy threshold in kcal/mol
    :param temperature:
        Temperature in Celsius.
    :param weight:
        How much to weigh this :any:`Constraint`.
    :param score_transfer_function:
        See :py:data:`Constraint.score_transfer_function`.
    :param description:
        Long description of constraint suitable for putting into constraint report.
    :param short_description:
        Short description of constraint suitable for logging to stdout.
    :param threaded:
        Whether to test the each pair of :any:`Strand`'s in parallel.
    :param pairs:
        Pairs of :any:`Strand`'s to compare; if not specified, checks all pairs.
    :param parameters_filename:
        Name of parameters file for ViennaRNA;
        default is same as :py:meth:`vienna_nupack.rna_duplex_multiple`
    :return:
        The :any:`StrandPairsConstraint`.
    """

    if description is None:
        description = f'RNAduplex energy for some strand pairs exceeds {threshold} kcal/mol'

    from dsd.stopwatch import Stopwatch

    num_threads = cpu_count() - 1  # this seems to be slightly faster than using all cores
    # we use ThreadPool instead of pathos because we're farming this out to processes through
    # subprocess module anyway, no need for pathos to boot up separate processes or serialize through dill
    thread_pool = ThreadPool(processes=num_threads)

    def calculate_energies_unthreaded(sequence_pairs: Sequence[Tuple[str, str]]) -> List[float]:
        return dv.rna_duplex_multiple(sequence_pairs, logger, temperature, parameters_filename)

    def calculate_energies(sequence_pairs: Sequence[Tuple[str, str]]) -> List[float]:
        if threaded and len(sequence_pairs) > 1:
            lists_of_sequence_pairs = chunker(sequence_pairs, num_chunks=num_threads)
            lists_of_energies = thread_pool.map(calculate_energies_unthreaded, lists_of_sequence_pairs)
            energies = _flatten(lists_of_energies)
        else:
            energies = calculate_energies_unthreaded(sequence_pairs)
        return energies

    def evaluate(strand_pairs: Iterable[Tuple[Strand, Strand]]) -> List[Tuple[OrderedSet[Domain], float]]:
        stopwatch: Optional[Stopwatch] = Stopwatch()  # noqa
        # stopwatch = None  # uncomment to not log time

        sequence_pairs = [(s1.sequence(), s2.sequence()) for s1, s2 in strand_pairs]
        energies = calculate_energies(sequence_pairs)

        domain_sets_weights: List[Tuple[OrderedSet[Domain], float]] = []
        for (strand1, strand2), energy in zip(strand_pairs, energies):
            excess = energy_excess(energy, threshold)
            if excess > 0.0:
                domain_set_weights = (
                    OrderedSet(strand1.unfixed_domains() + strand2.unfixed_domains()), excess)
                domain_sets_weights.append(domain_set_weights)

        if stopwatch is not None:
            stopwatch.stop()
            logger.debug(f'*** rna_duplex_strand_pairs_constraint ***')
            logger.debug(f'*   description: {description}')
            logger.debug(f'*   evaluated {len(sequence_pairs)} pairs of strands')
            logger.debug(f'*   total time to evaluate: {stopwatch}')
            logger.debug(f'*   energies: {sorted(energies)}')

        return domain_sets_weights

    def summary(strand_pairs: Iterable[Tuple[Strand, Strand]],
                report_only_violations: bool) -> ConstraintReport:
        num_violations = 0
        num_checks = 0

        max_name_length = max(len(strand.name) for strand in _flatten(strand_pairs))

        # distinguish between pairs in which both strands are fixed (so cannot remove violation)
        # versus those pairs in which at least one element of the pair is unfixed
        both_fixed_pairs = [(s1, s2) for s1, s2 in strand_pairs if s1.fixed and s2.fixed]
        one_unfixed_pairs = [(s1, s2) for s1, s2 in strand_pairs if not (s1.fixed and s2.fixed)]

        summaries = []

        for pairs_to_check, header_name in [(one_unfixed_pairs, 'pairs with at least one unfixed'),
                                            (both_fixed_pairs, 'pairs with both fixed')]:
            if len(pairs_to_check) > 0:
                report = _summary_of_pairs(pairs_to_check, report_only_violations, max_name_length)
                summary_ = _small_header(header_name, "=") + f'\n{report.content}\n'
                num_violations += report.num_violations
                num_checks += report.num_checks
                summaries.append(summary_)

        content = ''.join(summaries)
        report = ConstraintReport(constraint=None, content=content,
                                  num_violations=num_violations, num_checks=num_checks)
        return report

    def _summary_of_pairs(strand_pairs: Iterable[Tuple[Strand, Strand]], report_only_violations: bool,
                          max_name_length: int) -> ConstraintReport:
        sequence_pairs = [(s1.sequence(), s2.sequence()) for s1, s2 in strand_pairs]
        energies = calculate_energies(sequence_pairs)

        strand_pairs_energies = zip(strand_pairs, energies)

        num_checks = len(energies)
        num_violations = 0
        lines_and_excesses: List[Tuple[str, float]] = []
        for (strand1, strand2), energy in strand_pairs_energies:
            excess = energy_excess(energy, threshold)
            passed = excess <= 0.0
            if not passed:
                num_violations += 1
            if not report_only_violations or (report_only_violations and not passed):
                line = (f'strands '
                        f'{strand1.name:{max_name_length}}, '
                        f'{strand2.name:{max_name_length}}: '
                        f'{energy:6.2f} kcal/mol'
                        f'{"" if passed else "  **violation**"}')
                lines_and_excesses.append((line, excess))

        # put in descending order of excess
        lines_and_excesses.sort(key=lambda line_and_excess: line_and_excess[1], reverse=True)

        lines = (line for line, _ in lines_and_excesses)
        content = '\n'.join(lines)
        report = ConstraintReport(constraint=None, content=content,
                                  num_violations=num_violations, num_checks=num_checks)
        return report

    pairs_tuple = None
    if pairs is not None:
        pairs_tuple = tuple(pairs)

    return StrandPairsConstraint(description=description,
                                 short_description=short_description,
                                 weight=weight,
                                 score_transfer_function=score_transfer_function,
                                 evaluate=evaluate,
                                 summary=summary,
                                 pairs=pairs_tuple)


def energy_excess(energy: float, threshold: float) -> float:
    excess = threshold - energy
    return excess


def energy_excess_domains(energy: float,
                          threshold: Union[float, Dict[Tuple[DomainPool, DomainPool], float]],
                          domain1: Domain, domain2: Domain) -> float:
    threshold_value = 0.0  # noqa; warns that variable isn't used even though it clearly is
    if isinstance(threshold, Number):
        threshold_value = threshold
    elif isinstance(threshold, dict):
        threshold_value = threshold[(domain1.pool, domain2.pool)]
    excess = threshold_value - energy
    return excess


def rna_cofold_strand_pairs_constraint(
        threshold: float,
        temperature: float = dv.default_temperature,
        weight: float = 1.0,
        score_transfer_function: Callable[[float], float] = default_score_transfer_function,
        description: Optional[str] = None,
        short_description: str = 'rna_dup_strand_pairs',
        threaded: bool = False,
        pairs: Optional[Iterable[Tuple[Strand, Strand]]] = None,
        parameters_filename: str = dv.default_vienna_rna_parameter_filename) \
        -> StrandPairsConstraint:
    """
    Returns constraint that checks given pairs of :any:`Strand`'s for excessive interaction using
    Vienna RNA's RNAduplex executable.

    :param threshold:
        Energy threshold in kcal/mol
    :param temperature:
        Temperature in Celsius.
    :param weight:
        How much to weigh this :any:`Constraint`.
    :param score_transfer_function:
        See :py:data:`Constraint.score_transfer_function`.
    :param description:
        Long description of constraint suitable for putting into constraint report.
    :param short_description:
        Short description of constraint suitable for logging to stdout.
    :param threaded:
        Whether to test the each pair of :any:`Strand`'s in parallel.
    :param pairs:
        Pairs of :any:`Strand`'s to compare; if not specified, checks all pairs.
    :param parameters_filename:
        Name of parameters file for ViennaRNA;
        default is same as :py:meth:`vienna_nupack.rna_duplex_multiple`
    :return:
        The :any:`StrandPairsConstraint`.
    """

    if description is None:
        description = f'RNAcofold energy for some strand pairs exceeds {threshold} kcal/mol'

    from dsd.stopwatch import Stopwatch

    num_threads = cpu_count() - 1  # this seems to be slightly faster than using all cores
    thread_pool = ThreadPool(processes=num_threads)

    def calculate_energies_unthreaded(sequence_pairs: Sequence[Tuple[str, str]]) -> List[float]:
        return dv.rna_cofold_multiple(sequence_pairs, logger, temperature, parameters_filename)

    def calculate_energies(sequence_pairs: Sequence[Tuple[str, str]]) -> List[float]:
        if threaded and len(sequence_pairs) > 1:
            lists_of_sequence_pairs = chunker(sequence_pairs, num_chunks=num_threads)
            lists_of_energies = thread_pool.map(calculate_energies_unthreaded, lists_of_sequence_pairs)
            energies = _flatten(lists_of_energies)
        else:
            energies = calculate_energies_unthreaded(sequence_pairs)
        return energies

    def evaluate(strand_pairs: Iterable[Tuple[Strand, Strand]]) -> List[Tuple[OrderedSet[Domain], float]]:
        stopwatch: Optional[Stopwatch] = Stopwatch()  # noqa
        # stopwatch = None  # uncomment to not log time

        sequence_pairs = [(s1.sequence(), s2.sequence()) for s1, s2 in strand_pairs]
        domain_sets_weights: List[Tuple[OrderedSet[Domain], float]] = []
        # domain_sets_weights: List[Tuple[OrderedSet[Domain], float]] = []

        energies = calculate_energies(sequence_pairs)

        for (strand1, strand2), energy in zip(strand_pairs, energies):
            excess = energy_excess(energy, threshold)
            if excess > 0.0:
                domain_set_weights = (OrderedSet(strand1.unfixed_domains() + strand2.unfixed_domains()),
                                      excess)
                domain_sets_weights.append(domain_set_weights)

        if stopwatch is not None:
            stopwatch.stop()
            logger.debug(f'*** rna_cofold_strand_pairs_constraint ***')
            logger.debug(f'*   description: {description}')
            logger.debug(f'*   evaluated {len(sequence_pairs)} pairs of strands')
            logger.debug(f'*   total time to evaluate: {stopwatch}')
            logger.debug(f'*   energies: {sorted(energies)}')

        return domain_sets_weights

    def summary(strand_pairs: Iterable[Tuple[Strand, Strand]],
                report_only_violations: bool) -> ConstraintReport:
        sequence_pairs = [(s1.sequence(), s2.sequence()) for s1, s2 in strand_pairs]
        energies = calculate_energies(sequence_pairs)
        max_name_length = max(len(strand.name) for strand in _flatten(strand_pairs))
        strand_pairs_energies = zip(strand_pairs, energies)

        num_checks = len(energies)
        num_violations = 0
        lines: List[str] = []
        for (strand1, strand2), energy in strand_pairs_energies:
            passed = energy_excess(energy, threshold) <= 0.0
            if not passed:
                num_violations += 1
            if not report_only_violations or (report_only_violations and not passed):
                line = (f'strands '
                        f'{strand1.name:{max_name_length}}, '
                        f'{strand2.name:{max_name_length}}: '
                        f'{energy:6.2f} kcal/mol'
                        f'{"" if passed else "  **violation**"}')
                lines.append(line)

        if not report_only_violations:
            lines.sort(key=lambda line_: ' **violation**' not in line_)  # put violations first

        return ConstraintReport(constraint=None, content='\n'.join(lines),
                                num_violations=num_violations, num_checks=num_checks)

    pairs_tuple = None
    if pairs is not None:
        pairs_tuple = tuple(pairs)

    return StrandPairsConstraint(description=description,
                                 short_description=short_description,
                                 weight=weight,
                                 score_transfer_function=score_transfer_function,
                                 evaluate=evaluate,
                                 summary=summary,
                                 pairs=pairs_tuple)


def _all_pairs_domain_sequences_complements_names_from_domains(
        domain_pairs: Iterable[Tuple[Domain, Domain]]) \
        -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]], List[Tuple[Domain, Domain]]]:
    """
    :param domain_pairs:
        Domain pairs.
    :return:
        pair consisting of two lists, each of length 4 times as long as `domain_pairs`.
        Each pair in `domain_pairs` is associated to the 4 combinations of WC complementing (or not)
        the sequences of each Domain.
        - sequence_pairs: the sequences (appropriated complemented or not)
        - names: the names (appropriately *'d or not)
    """
    sequence_pairs: List[Tuple[str, str]] = []
    names: List[Tuple[str, str]] = []
    domains: List[Tuple[Domain, Domain]] = []
    for d1, d2 in domain_pairs:
        if d1 == d2:
            # don't check d-d* or d*-d in this case, but do check d-d and d*-d*
            starred_each = [(False, False), (True, True)]
        else:
            starred_each = [(False, False), (True, True), (False, True), (True, False)]
        for starred1, starred2 in starred_each:
            seq1 = d1.concrete_sequence(starred1)
            seq2 = d2.concrete_sequence(starred2)
            name1 = d1.get_name(starred1)
            name2 = d2.get_name(starred2)
            sequence_pairs.append((seq1, seq2))
            names.append((name1, name2))
            domains.append((d1, d2))
    return sequence_pairs, names, domains


def _flatten(list_of_lists: Iterable[Iterable[T]]) -> List[T]:
    #  Flatten one level of nesting
    return list(itertools.chain.from_iterable(list_of_lists))


def rna_duplex_domain_pairs_constraint(
        threshold: float,
        temperature: float = dv.default_temperature,
        weight: float = 1.0,
        score_transfer_function: Callable[[float], float] = lambda x: x,
        description: Optional[str] = None,
        short_description: str = 'rna_dup_dom_pairs',
        pairs: Optional[Iterable[Tuple[Domain, Domain]]] = None,
        parameters_filename: str = dv.default_vienna_rna_parameter_filename) \
        -> DomainPairsConstraint:
    """
    Returns constraint that checks given pairs of :any:`Domain`'s for excessive interaction using
    Vienna RNA's RNAduplex executable.

    :param threshold:
        energy threshold
    :param temperature:
        temperature in Celsius
    :param weight:
        how much to weigh this :any:`Constraint`
    :param score_transfer_function:
        See :py:data:`Constraint.score_transfer_function`.
    :param description:
        long description of constraint suitable for printing in report file
    :param short_description:
        short description of constraint suitable for logging to stdout
    :param pairs:
        pairs of :any:`Domain`'s to compare; if not specified, checks all pairs
    :param parameters_filename:
        name of parameters file for ViennaRNA; default is
        same as :py:meth:`vienna_nupack.rna_duplex_multiple`
    :return:
        constraint
    """

    if description is None:
        description = f'RNAduplex energy for some strand pairs exceeds {threshold} kcal/mol'

    def evaluate(domain_pairs: Iterable[Tuple[Domain, Domain]]) -> List[Tuple[OrderedSet[Domain], float]]:
        if any(d1.sequence is None or d2.sequence is None for d1, d2 in domain_pairs):
            raise ValueError('cannot evaluate domains unless they have sequences assigned')
        sequence_pairs, _, _ = _all_pairs_domain_sequences_complements_names_from_domains(domain_pairs)
        domain_sets_weights: List[Tuple[OrderedSet[Domain], float]] = []
        energies = dv.rna_duplex_multiple(sequence_pairs, logger, temperature, parameters_filename)
        for (domain1, domain2), energy in zip(domain_pairs, energies):
            excess = threshold - energy
            if excess > 0.0:
                domain_set_weights = (OrderedSet([domain1, domain2]), excess)
                domain_sets_weights.append(domain_set_weights)
        return domain_sets_weights

    def summary(domain_pairs: Iterable[Tuple[Domain, Domain]],
                report_only_violations: bool) -> ConstraintReport:
        sequence_pairs, domain_name_pairs, domains = \
            _all_pairs_domain_sequences_complements_names_from_domains(domain_pairs)
        energies = dv.rna_duplex_multiple(sequence_pairs, logger, temperature, parameters_filename)
        max_name_length = max(len(name) for name in _flatten(domain_name_pairs))

        num_checks = len(energies)
        num_violations = 0
        lines: List[str] = []
        for (domain1, domain2), (name1, name2), energy in zip(domains, domain_name_pairs, energies):
            passed = energy_excess_domains(energy, threshold, domain1, domain2) <= 0.0
            if not passed:
                num_violations += 1
            if not report_only_violations or (report_only_violations and not passed):
                line = (f'domains '
                        f'{name1:{max_name_length}}, '
                        f'{name2:{max_name_length}}: '
                        f'{energy:6.2f} kcal/mol'
                        f'{"" if passed else "  **violation**"}')
                lines.append(line)

        if not report_only_violations:
            lines.sort(key=lambda line_: ' **violation**' not in line_)  # put violations first

        content = '\n'.join(lines)
        report = ConstraintReport(constraint=None, content=content,
                                  num_violations=num_violations, num_checks=num_checks)
        return report

    pairs_tuple = None
    if pairs is not None:
        pairs_tuple = tuple(pairs)

    return DomainPairsConstraint(description=description,
                                 short_description=short_description,
                                 weight=weight,
                                 score_transfer_function=score_transfer_function,
                                 evaluate=evaluate,
                                 summary=summary,
                                 pairs=pairs_tuple)


#########################################################################################
# ComplexConstraints defined below here
#########################################################################################


@dataclass(frozen=True, eq=False)
class ConstraintWithComplexes(Constraint[DesignPart], Generic[DesignPart]):
    complexes: Tuple[Complex, ...] = ()
    """
    List of complexes (tuples of :any:`Strand`'s) to check.
    """

    def generate_summary(self, design_part: DesignPart, report_only_violations: bool) -> str:
        raise NotImplementedError('subclasses of ConstraintWithStrandPairs must implement generate_summary')


@dataclass(frozen=True, eq=False)  # type: ignore
class ComplexConstraint(ConstraintWithComplexes[Complex]):
    """Constraint that applies to a complex (tuple of :any:`Strand`'s).

    Unlike other types of :any:`Constraint`'s such as :any:`StrandConstraint` or :any:`StrandPairConstraint`,
    there is no default list of :any:`Complex`'s that a :any:`ComplexConstraint` is applied to. The list of
    :any:`Complex`'s must be specified manually in the constructor."""

    evaluate: Callable[[Complex],
                       float] = lambda _, __: 0.0
    """
    Evaluation to perform on complex (tuple of :any:`Strand`'s).
    Returns float indicating how much the constraint is violated,
    or 0.0 if the constraint is satisfied.
    """

    summary: Callable[[Complex],
                      str] = lambda _, __: _no_summary_string

    def __call__(self, strand_complex: Complex) -> float:
        excess = (self.evaluate)(strand_complex)  # noqa
        if excess < 0:
            return 0.0
        score = (self.score_transfer_function)(excess)  # noqa
        return score

    def generate_summary(self, strand_complex: Complex, report_only_violations: bool) -> str:
        return (self.summary)(strand_complex)  # noqa


def _alter_scores_by_transfer(sets_excesses: List[Tuple[OrderedSet[Domain], float]],
                              transfer_callback: Callable[[float], float]) \
        -> List[Tuple[OrderedSet[Domain], float]]:
    sets_weights: List[Tuple[OrderedSet[Domain], float]] = []
    for set_, excess in sets_excesses:
        if excess < 0:
            weight = 0.0
        else:
            weight = transfer_callback(excess)
        sets_weights.append((set_, weight))
    return sets_weights


@dataclass(frozen=True, eq=False)  # type: ignore
class ComplexesConstraint(ConstraintWithComplexes[Iterable[Complex]]):
    """
    Similar to :any:`ComplexConstraint` but operates on a specified list of complexes
    (tuples of :any:`Strand`'s).
    """

    evaluate: Callable[[Iterable[Complex]],
                       List[Tuple[OrderedSet[Domain], float]]] = lambda _: []
    """
    Check to perform on an iterable of complexes (tuples of :any:`Strand`'s).
    Returns True if and only if the all complexes in the input iterable satisfy the constraint.
    """

    summary: Callable[[Iterable[Complex], bool],
                      ConstraintReport] = lambda _: _no_summary_string

    def __call__(self, complexes: Iterable[Complex]) \
            -> List[Tuple[OrderedSet[Domain], float]]:
        sets_excesses = (self.evaluate)(strands)  # noqa
        sets_scores = _alter_scores_by_transfer(sets_excesses, self.score_transfer_function)
        return sets_scores

    def generate_summary(self, complexes: Iterable[Complex],
                         report_only_violations: bool) -> ConstraintReport:
        return (self.summary)(strands, report_only_violations)  # noqa


class _AdjacentDuplexType(Enum):
    # Refer to comments under BaseTypePair for notation reference
    #
    #
    # Domain definitions:
    #   All AdjacentDuplexType are with reference to domain c.
    #   AdjacentDuplexType is agnostic to the ends of domain c.
    #   Hence, c is written as #-----# in each of the AdjacentDuplexType
    #   variants.
    #
    #   c* - complement of c   (must exist)
    #   d* - 5' neighbor of c* (does not neccessarily exist)
    #   d  - complement of d*  (does not neccessarily exist)
    #   e  - 5' neighbor of d  (does not neccessarily exist)
    #   e* - complement of e   (does not neccessarily exist)
    #
    #                          # #
    #                          |-|
    #                       e* |-| e (bound)
    #                          |-|
    #                          # #
    #                       c  ? # d
    #                    #-----# #-----#
    #                     |||||   |||||
    #                    #-----###-----#
    #                       c*    d*
    #
    #   Note: if "?" was a "#" (meaning e* is adjacent to c), then then
    #   it would be a three arm junction
    #
    ###########################################################################

    # d* does not exist
    #                       c
    #                    #-----#
    #                     |||||
    #                    #-----]
    #                       c*
    BOTTOM_RIGHT_EMPTY = auto()

    # d* exist, but d does not exist
    #                       c
    #                    #-----#
    #                     |||||
    #                    #-----##----#
    #                       c*    d*
    BOTTOM_RIGHT_DANGLE = auto()

    # d* and d exist, but e does not exist
    # d is is the 5' end of the strand
    #                       c     d
    #                    #-----#[----#
    #                     |||||  ||||
    #                    #-----##----#
    #                       c*    d*
    TOP_RIGHT_5P = auto()

    # d* and d and e exist, but e* does not exist
    #                           #
    #                           |
    #                           | e (unbound)
    #                           |
    #                           #
    #                       c   | d
    #                    #-----#+---#
    #                     ||||| ||||
    #                    #-----#----#
    #                       c*    d*
    TOP_RIGHT_OVERHANG = auto()

    # d* and d and e and e* exist
    #
    # ? on 3p end of c domain because this case is agnostic to
    # whether c and e* are connected
    #
    #                          # #
    #                          |-|
    #                       e* |-| e (bound)
    #                          |-|
    #                          # #
    #                       c  ? # d
    #                    #-----# #---#
    #                     |||||  ||||
    #                    #-----###---#
    #                       c*    d*
    TOP_RIGHT_BOUND_OVERHANG = auto()


default_interior_to_strand_probability = 0.98
"""Default probability threshold for :py:attr:`BasePairType.INTERIOR_TO_STRAND`"""
default_adjacent_to_exterior_base_pair = 0.95
"""Default probability threshold for :py:attr:`BasePairType.ADJACENT_TO_EXTERIOR_BASE_PAIR`"""

default_blunt_end_probability = 0.33
"""Default probability threshold for :py:attr:`BasePairType.BLUNT_END`"""

default_nick_3p_probability = 0.79
"""Default probability threshold for :py:attr:`BasePairType.NICK_3P`"""

default_nick_5p_probability = 0.73
"""Default probability threshold for :py:attr:`BasePairType.NICK_5P`"""

default_dangle_3p_probability = 0.51
"""Default probability threshold for :py:attr:`BasePairType.DANGLE_3P`"""

default_dangle_5p_probability = 0.57
"""Default probability threshold for :py:attr:`BasePairType.DANGLE_5P`"""

default_dangle_5p_3p_probability = 0.73
"""Default probability threshold for :py:attr:`BasePairType.DANGLE_5P_3P`"""

default_overhang_on_this_strand_3p_probability = 0.82
"""Default probability threshold for :py:attr:`BasePairType.OVERHANG_ON_THIS_STRAND_3P`"""

default_overhang_on_this_strand_5p_probability = 0.79
"""Default probability threshold for :py:attr:`BasePairType.OVERHANG_ON_THIS_STRAND_5P`"""

default_overhang_on_adjacent_strand_3p_probability = 0.55
"""Default probability threshold for :py:attr:`BasePairType.OVERHANG_ON_ADJACENT_STRAND_3P`"""

default_overhang_on_adjacent_strand_5p_probability = 0.49
"""Default probability threshold for :py:attr:`BasePairType.OVERHANG_ON_ADJACENT_STRAND_5P`"""

default_overhang_on_both_strand_3p_probability = 0.61
"""Default probability threshold for :py:attr:`BasePairType.OVERHANG_ON_BOTH_STRANDS_3P`"""

default_overhang_on_both_strand_5p_probability = 0.55
"""Default probability threshold for :py:attr:`BasePairType.OVERHANG_ON_BOTH_STRANDS_5P`"""

default_three_arm_junction_probability = 0.69
"""Default probability threshold for :py:attr:`BasePairType.THREE_ARM_JUNCTION`"""

default_four_arm_junction_probability = 0.84
"""Default probability threshold for :py:attr:`BasePairType.FOUR_ARM_JUNCTION`"""

default_five_arm_junction_probability = 0.77
"""Default probability threshold for :py:attr:`BasePairType.FIVE_ARM_JUNCTION`"""

default_mismatch_probability = 0.76
"""Default probability threshold for :py:attr:`BasePairType.MISMATCH`"""

default_bulge_loop_3p_probability = 0.69
"""Default probability threshold for :py:attr:`BasePairType.BULGE_LOOP_3P`"""

default_bulge_loop_5p_probability = 0.65
"""Default probability threshold for :py:attr:`BasePairType.BULGE_LOOP_5P`"""

default_unpaired_probability = 0.95
"""Default probability threshold for :py:attr:`BasePairType.UNPAIRED`"""

default_other_probability = 0.70
"""Default probability threshold for :py:attr:`BasePairType.OTHER`"""


class BasePairType(Enum):
    """
    Represents different configurations for a base pair and it's immediate
    neighboring base pairs (or lack thereof).

    **Notation**:

    * "#" indicates denotes the ends of a domain.
      They can either be the end of a strand or they could be connected to another domain.
    * "]" and "[" indicates 5' ends of strand
    * ">" and "<" indicates 3' ends of a strand
    * "-" indicates a base (number of these are not important).
    * "|" indicates a bases are bound (forming a base pair).
      Any "-" not connected by "|" is unbound

    **Domain Example**:

    The following represents an unbound domain of length 5

    .. code-block:: none

        #-----#

    The following represents bound domains of length 5

    .. code-block:: none

        #-----#
         |||||
        #-----#


    Ocassionally, domains will be vertical in the case of overhangs.
    In this case, "-" and "|" have opposite meanings

    **Vertical Domain Example**:

    .. code-block:: none

        # #
        |-|
        |-|
        |-|
        |-|
        |-|
        # #

    **Formatting**:

    * Top strands have 5' end on left side and 3' end on right side
    * Bottom strand have 3' end on left side and 5' end on right side

    **Strand Example**:

    .. code-block:: none

      strand0: a-b-c-d
      strand1: d*-b*-c*-a*

                  a      b      c      d
      strand0  [-----##-----##-----##----->
                |||||  |||||  |||||  |||||
      strand1  <-----##-----##-----##-----]
                  a*     b*     c*     d*

    **Consecutive "#"**:

    In some cases, extra "#" are needed to to make space for ascii art.
    We consider any consecutive "#"s to be equivalent "##".
    The following is consider equivalent to the example above

    .. code-block:: none

                  a       b        c      d
      strand0  [-----###-----####-----##----->
                |||||   |||||    |||||  |||||
      strand1  <-----###-----####-----##-----]
                  a*      b*       c*     d*

    Note that only consecutive "#"s is consider equivalent to "##".
    The following example is not equivalent to the strands above because
    the "#  #" between b and c are seperated by spaces, so they are
    not equivalent to "##", meaning that b and c neednot be adjacent.
    Note that while b and c need not be adjacent, b* and c* are still
    adjacent because they are seperated by consecutive "#"s with no
    spaces in between.

    .. code-block:: none

                  a       b        c      d
      strand0  [-----###-----#  #-----##----->
                |||||   |||||    |||||  |||||
      strand1  <-----###-----####-----##-----]
                  a*      b*       c*     d*
    """

    INTERIOR_TO_STRAND = auto()
    """
    Base pair is located inside of a strand but not next
    to a base pair that resides on the end of a strand.

    Similar base-pairing probability compared to :py:attr:`ADJACENT_TO_EXTERIOR_BASE_PAIR` but usually breathes less.

    .. code-block:: none

        #-----##-----#
         |||||  |||||
        #-----##-----#
             ^
             |
         base pair

    """

    ADJACENT_TO_EXTERIOR_BASE_PAIR = auto()
    """
    Base pair is located inside of a strand and next
    to a base pair that resides on the end of a strand.

    Similar base-pairing probability compared to :py:attr:`INTERIOR_TO_STRAND` but usually breathes more.

    .. code-block:: none

        #-----#
         |||||
        #-----]
            ^
            |
        base pair

    or

    .. code-block:: none


        #----->
         |||||
        #-----#
            ^
            |
        base pair
    """

    BLUNT_END = auto()
    """
    Base pair is located at the end of both strands.

    .. code-block:: none

        #----->
         |||||
        #-----]
             ^
             |
         base pair
    """

    NICK_3P = auto()
    """
    Base pair is located at a nick involving the 3' end of the strand.

    .. code-block:: none

        #----->[-----#
         |||||  |||||
        #-----##-----#
             ^
             |
         base pair

    """

    NICK_5P = auto()
    """
    Base pair is located at a nick involving the 3' end of the strand.

    .. code-block:: none

        #-----##-----#
         |||||  |||||
        #-----]<-----#
             ^
             |
         base pair
    """

    DANGLE_3P = auto()
    """
    Base pair is located at the end of a strand with a dangle on the
    3' end.

    .. code-block:: none

        #-----##----#
         |||||
        #-----]
             ^
             |
         base pair
    """

    DANGLE_5P = auto()
    """
    Base pair is located at the end of a strand with a dangle on the
    5' end.

    .. code-block:: none

        #----->
         |||||
        #-----##----#
             ^
             |
         base pair
    """

    DANGLE_5P_3P = auto()
    """
    Base pair is located with dangle at both the 3' and 5' end.

    .. code-block:: none

        #-----##----#
         |||||
        #-----##----#
             ^
             |
         base pair
    """

    OVERHANG_ON_THIS_STRAND_3P = auto()
    """
    Base pair is next to a overhang on the 3' end.

    .. code-block:: none

              #
              |
              |
              |
              #
        #-----# #-----#
         |||||   |||||
        #-----###-----#
             ^
             |
         base pair
    """

    OVERHANG_ON_THIS_STRAND_5P = auto()
    """
    Base pair is next to a overhang on the 5' end.

    .. code-block:: none

         base pair
             |
             v
        #-----###-----#
         |||||   |||||
        #-----# #-----#
              #
              |
              |
              |
              #
    """

    OVERHANG_ON_ADJACENT_STRAND_3P = auto()
    """
    Base pair 3' end interfaces with an overhang.

    The adjacent base pair type is :py:attr:`OVERHANG_ON_THIS_STRAND_5P`

    .. code-block:: none

                #
                |
                |
                |
                #
        #-----# #---#
         |||||   |||
        #-----###---#
             ^
             |
         base pair
    """

    OVERHANG_ON_ADJACENT_STRAND_5P = auto()
    """
    Base pair 5' end interfaces with an overhang.

    The adjacent base pair type is :py:attr:`OVERHANG_ON_THIS_STRAND_3P`

    .. code-block:: none

         base pair
             |
             v
        #-----###-----#
         |||||   |||||
        #-----# #-----#
                #
                |
                |
                |
                #
    """

    OVERHANG_ON_BOTH_STRANDS_3P = auto()
    """
    Base pair's 3' end is an overhang and adjacent strand also has an overhang.

    .. code-block:: none

              # #
              | |
              | |
              | |
              # #
        #-----# #---#
         |||||  ||||
        #-----###---#
             ^
             |
         base pair
    """

    OVERHANG_ON_BOTH_STRANDS_5P = auto()
    """
    Base pair's 5' end is an overhang and adjacent strand also has an overhang.

    .. code-block:: none

         base pair
             |
             v
        #-----###-----#
         |||||   |||||
        #-----# #-----#
              # #
              | |
              | |
              | |
              # #
    """

    THREE_ARM_JUNCTION = auto()
    """
    Base pair is located next to a three-arm-junction.

    .. code-block:: none


              # #
              |-|
              |-|
              |-|
              # #
        #-----# #---#
         |||||  ||||
        #-----###---#
             ^
             |
         base pair
    """

    FOUR_ARM_JUNCTION = auto()
    """
    TODO: Currently, this case isn't actually detected (considered as :py:attr:`OTHER`).

    Base pair is located next to a four-arm-junction (e.g. Holliday junction).

    .. code-block:: none

              # #
              |-|
              |-|
              |-|
              # #
        #-----# #-----#
         |||||   |||||
        #-----# #-----#
              # #
              |-|
              |-|
              |-|
              # #
    """

    FIVE_ARM_JUNCTION = auto()
    """
    TODO: Currently, this case isn't actually detected (considered as :py:attr:`OTHER`).

    Base pair is located next to a five-arm-junction.
    """

    MISMATCH = auto()
    """
    TODO: Currently, this case isn't actually detected (considered as :py:attr:`DANGLE_5P_3P`).

    Base pair is located next to a mismatch.

    .. code-block:: none

        #-----##-##-----#
         |||||     |||||
        #-----##-##-----#
             ^
             |
         base pair
    """

    BULGE_LOOP_3P = auto()
    """
    TODO: Currently, this case isn't actually detected (considered as :py:attr:`OVERHANG_ON_BOTH_STRANDS_3P`).

    Base pair is located next to a mismatch.

    .. code-block:: none

        #-----##-##-----#
         |||||     |||||
        #-----#####-----#
             ^
             |
         base pair
    """

    BULGE_LOOP_5P = auto()
    """
    TODO: Currently, this case isn't actually detected (considered as :py:attr:`OVERHANG_ON_BOTH_STRANDS_5P`).

    Base pair is located next to a mismatch.

    .. code-block:: none

        #-----#####-----#
         |||||     |||||
        #-----##-##-----#
             ^
             |
         base pair
    """

    UNPAIRED = auto()
    """
    Base is unpaired.

    Probabilities specify how unlikely a base is to be paired with another base.
    """

    OTHER = auto()
    """
    Other base pair types.
    """

    def default_pair_probability(self) -> float:
        if self is BasePairType.INTERIOR_TO_STRAND:
            return default_interior_to_strand_probability
        elif self is BasePairType.ADJACENT_TO_EXTERIOR_BASE_PAIR:
            return default_adjacent_to_exterior_base_pair
        elif self is BasePairType.BLUNT_END:
            return default_blunt_end_probability
        elif self is BasePairType.NICK_3P:
            return default_nick_3p_probability
        elif self is BasePairType.NICK_5P:
            return default_nick_5p_probability
        elif self is BasePairType.DANGLE_3P:
            return default_dangle_3p_probability
        elif self is BasePairType.DANGLE_5P:
            return default_dangle_5p_probability
        elif self is BasePairType.DANGLE_5P_3P:
            return default_dangle_5p_3p_probability
        elif self is BasePairType.OVERHANG_ON_THIS_STRAND_3P:
            return default_overhang_on_this_strand_3p_probability
        elif self is BasePairType.OVERHANG_ON_THIS_STRAND_5P:
            return default_overhang_on_this_strand_5p_probability
        elif self is BasePairType.OVERHANG_ON_ADJACENT_STRAND_3P:
            return default_overhang_on_adjacent_strand_3p_probability
        elif self is BasePairType.OVERHANG_ON_ADJACENT_STRAND_5P:
            return default_overhang_on_adjacent_strand_5p_probability
        elif self is BasePairType.OVERHANG_ON_BOTH_STRANDS_3P:
            return default_overhang_on_both_strand_3p_probability
        elif self is BasePairType.OVERHANG_ON_BOTH_STRANDS_5P:
            return default_overhang_on_both_strand_5p_probability
        elif self is BasePairType.THREE_ARM_JUNCTION:
            return default_three_arm_junction_probability
        elif self is BasePairType.FOUR_ARM_JUNCTION:
            return default_four_arm_junction_probability
        elif self is BasePairType.FIVE_ARM_JUNCTION:
            return default_five_arm_junction_probability
        elif self is BasePairType.OTHER:
            return default_other_probability
        elif self is BasePairType.UNPAIRED:
            return default_unpaired_probability
        elif self is BasePairType.BULGE_LOOP_3P:
            return default_bulge_loop_3p_probability
        elif self is BasePairType.BULGE_LOOP_5P:
            return default_bulge_loop_5p_probability
        elif self is BasePairType.MISMATCH:
            return default_mismatch_probability
        else:
            assert False


@dataclass
class StrandDomainAddress:
    """An addressing scheme for specifying a domain on a strand.
    """

    strand: Strand
    """strand to index
    """

    domain_idx: int
    """order in which domain appears in :py:data:`StrandDomainAddress.strand`
    """

    def neighbor_5p(self) -> Optional['StrandDomainAddress']:
        """Returns 5' domain neighbor. If domain is 5' end of strand, returns None

        :return: StrandDomainAddress of 5' neighbor or None if no 5' neighbor
        :rtype: Optional[StrandDomainAddress]
        """
        idx = self.domain_idx - 1
        if idx >= 0:
            return StrandDomainAddress(self.strand, idx)
        else:
            return None

    def neighbor_3p(self) -> Optional['StrandDomainAddress']:
        """Returns 3' domain neighbor. If domain is 3' end of strand, returns None

        :return: StrandDomainAddress of 3' neighbor or None if no 3' neighbor
        :rtype: Optional[StrandDomainAddress]
        """
        idx = self.domain_idx + 1
        if idx < len(self.strand.domains):
            return StrandDomainAddress(self.strand, idx)
        else:
            return None

    def domain(self) -> Domain:
        """Returns domain referenced by this address.

        :return: domain
        :rtype: Domain
        """
        return self.strand.domains[self.domain_idx]

    def __hash__(self) -> int:
        return hash((self.strand, self.domain_idx))

    def __eq__(self, other):
        if isinstance(other, StrandDomainAddress):
            return self.strand == other.strand and self.domain_idx == other.domain_idx
        return False

    def __str__(self) -> str:
        return f'{{strand: {self.strand}, domain_idx: {self.domain_idx}}}'

    def __repr__(self) -> str:
        return self.__str__() + f' hash: {self.__hash__()}'


def _exterior_base_type_of_domain_3p_end(domain_addr: StrandDomainAddress,
                                         all_bound_domain_addresses: Dict[
                                             StrandDomainAddress, StrandDomainAddress]) -> BasePairType:
    """Returns the BasePairType that corresponds to the base pair that sits on the
    3' end of provided domain.

    :param domain_addr: The address of the domain that contains the interested
    :type domain_addr: StrandDomainAddress
    :param all_bound_domain_addresses: A mapping of all the domain pairs in complex
    :type all_bound_domain_addresses: Dict[StrandDomainAddress, StrandDomainAddress]
    :return: BasePairType of base pair on 3' end of domain
    :rtype: BasePairType
    """
    # Declare domain variables:
    #                              # #
    #                              |-|
    #                            ? |-| adjacent_5n_addr
    #                              |-|
    #                              # #
    #             domain_addr      ? #        adjacent_addr
    #    #-------------------------# #-------------------------------------#
    #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
    #    #-------------------------###-------------------------------------#
    #         complementary_addr              complementary_5n_addr
    assert domain_addr in all_bound_domain_addresses
    complementary_addr = all_bound_domain_addresses[domain_addr]
    complementary_5n_addr = complementary_addr.neighbor_5p()
    adjacent_addr: Optional[StrandDomainAddress] = None
    adjacent_5n_addr: Optional[StrandDomainAddress] = None

    # First assume BOTTOM_RIGHT_EMPTY
    #            domain_addr
    #    #-------------------------#
    #     |||||||||||||||||||||||||
    #    #-------------------------] <- Note this 3' end here
    #         complementary_addr
    adjacent_strand_type: _AdjacentDuplexType = _AdjacentDuplexType.BOTTOM_RIGHT_EMPTY

    if complementary_5n_addr is not None:
        #   Since complementary_5n_addr exists, assume BOTTOM_RIGHT_DANGLE
        #
        #            domain_addr
        #    #-------------------------#
        #     |||||||||||||||||||||||||
        #    #-------------------------###-------------------------------------#
        #     complementary_addr                complementary_5n_addr
        adjacent_strand_type = _AdjacentDuplexType.BOTTOM_RIGHT_DANGLE
        if complementary_5n_addr in all_bound_domain_addresses:
            # Since complementary_5n_addr is bound, meaning
            # adjacent_addr exist, assume TOP_RIGHT_5p
            #
            #             domain_addr                adjacent_addr
            #    #-------------------------# [-------------------------------------#
            #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
            #    #-------------------------###-------------------------------------#
            #          complementary_addr          complementary_5n_addr
            adjacent_strand_type = _AdjacentDuplexType.TOP_RIGHT_5P
            adjacent_addr = all_bound_domain_addresses[complementary_5n_addr]
            adjacent_5n_addr = adjacent_addr.neighbor_5p()
            if adjacent_5n_addr is not None:
                # Since adjacent_5n_addr exists, assume TOP_RIGHT_OVERHANG
                #
                #                                #
                #                                |
                #                                | adjacent_5n_addr
                #                                |
                #                                #
                #             domain_addr        #       adjacent_addr
                #    #-------------------------# #-------------------------------------#
                #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
                #    #-------------------------###-------------------------------------#
                #          complementary_addr          complementary_5n_addr
                adjacent_strand_type = _AdjacentDuplexType.TOP_RIGHT_OVERHANG
                if adjacent_5n_addr in all_bound_domain_addresses:
                    # Since adjacent_5n_addr is bound, two possible cases:

                    if domain_addr == adjacent_5n_addr:
                        # Since domain_addr and adjacent_5n_addr
                        # are the same, then this must be an internal base pair
                        #
                        #   domain_addr == adjacent_5n_addr        adjacent_addr
                        #    #-------------------------###-------------------------------------#
                        #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
                        #    #-------------------------###-------------------------------------#
                        #        complementary_addr            complementary_5n_addr

                        # Assuming non-competitive, then this must be internal base pair or
                        # if the domain is length 2 and the 5' type is not interior, then
                        # it is adjacent to exterior base pair type
                        domain = domain_addr.strand.domains[domain_addr.domain_idx]
                        domain_next_to_interior_base_pair = domain_addr.neighbor_5p() is not None and complementary_addr.neighbor_3p() is not None
                        if domain.length == 2 and not domain_next_to_interior_base_pair:
                            #   domain_addr == adjacent_5n_addr        adjacent_addr
                            #     |                                       |
                            #    [--###-------------------------------------#
                            #     ||   |||||||||||||||||||||||||||||||||||||
                            #    <--###-------------------------------------#
                            #     |                              |
                            # complementary_addr       complementary_5n_addr
                            return BasePairType.ADJACENT_TO_EXTERIOR_BASE_PAIR
                        else:
                            return BasePairType.INTERIOR_TO_STRAND
                    else:
                        # Since adjacent_5n_addr does not equal domain_addr,
                        # must be a bound overhang:
                        #
                        #                              # #
                        #                              |-|
                        #                            ? |-| adjacent_5n_addr
                        #                              |-|
                        #                              # #
                        #             domain_addr      ? #        adjacent_addr
                        #    #-------------------------# #-------------------------------------#
                        #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
                        #    #-------------------------###-------------------------------------#
                        #         complementary_addr            complementary_5n_addr
                        adjacent_strand_type = _AdjacentDuplexType.TOP_RIGHT_BOUND_OVERHANG

    domain_3n_addr = domain_addr.neighbor_3p()
    if domain_3n_addr is None:
        # domain_addr is at 3' end of strand
        #
        #            domain_addr
        #    #------------------------->
        #     |||||||||||||||||||||||||
        #    #-------------------------#
        #         complementary_addr

        if adjacent_strand_type is _AdjacentDuplexType.BOTTOM_RIGHT_EMPTY:
            #            domain_addr
            #    #------------------------->
            #     |||||||||||||||||||||||||
            #    #-------------------------]
            #         complementary_addr
            return BasePairType.BLUNT_END
        elif adjacent_strand_type is _AdjacentDuplexType.BOTTOM_RIGHT_DANGLE:
            #          domain_addr
            #    #------------------------->
            #     |||||||||||||||||||||||||
            #    #-------------------------###-------------------------------------#
            #       complementary_addr              complementary_5n_addr
            return BasePairType.DANGLE_5P
        elif adjacent_strand_type is _AdjacentDuplexType.TOP_RIGHT_5P:
            #             domain_addr                adjacent_addr
            #    #-------------------------> [-------------------------------------#
            #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
            #    #-------------------------###-------------------------------------#
            #     complementary_addr    complementary_5n_addr
            return BasePairType.NICK_3P
        elif adjacent_strand_type is _AdjacentDuplexType.TOP_RIGHT_OVERHANG:
            #                                #
            #                                |
            #                                | adjacent_5n_addr
            #                                |
            #                                #
            #             domain_addr        #        adjacent_addr
            #    #-------------------------> #-------------------------------------#
            #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
            #    #-------------------------###-------------------------------------#
            #     complementary_addr    complementary_5n_addr
            return BasePairType.OVERHANG_ON_ADJACENT_STRAND_3P
        elif adjacent_strand_type is _AdjacentDuplexType.TOP_RIGHT_BOUND_OVERHANG:
            #                              # #
            #                              |-|
            #                            ? |-| adjacent_5n_addr
            #                              |-|
            #                              # #
            #             domain_addr        #        adjacent_addr
            #    #-------------------------> #-------------------------------------#
            #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
            #    #-------------------------###-------------------------------------#
            #     complementary_addr    complementary_5n_addr
            # TODO: Possible case (nick n-arm junction)
            return BasePairType.OTHER
        else:
            # Shouldn't reach here
            assert False
    else:
        # domain_addr is not the 3' end of the strand
        #
        #            domain_addr              domain_3n_addr
        #    #-------------------------##-------------------------#

        if domain_3n_addr not in all_bound_domain_addresses:
            # domain_addr's 3' neighbor is an unbound overhang
            if adjacent_strand_type is _AdjacentDuplexType.BOTTOM_RIGHT_EMPTY:
                #            domain_addr             domain_3n_addr
                #    #-------------------------##-------------------------#
                #     |||||||||||||||||||||||||
                #    #-------------------------]
                #     complementary_addr
                return BasePairType.DANGLE_3P
            elif adjacent_strand_type is _AdjacentDuplexType.BOTTOM_RIGHT_DANGLE:
                #            domain_addr                 domain_3n_addr
                #    #-------------------------##-------------------------------------#
                #     |||||||||||||||||||||||||
                #    #-------------------------##-------------------------------------#
                #          complementary_addr            complementary_5n_addr
                return BasePairType.DANGLE_5P_3P
            elif adjacent_strand_type is _AdjacentDuplexType.TOP_RIGHT_5P:
                #                              #
                #                              |
                #               domain_3n_addr |
                #                              |
                #                              #
                #             domain_addr      #         adjacent_addr
                #    #-------------------------# [-------------------------------------#
                #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
                #    #-------------------------###-------------------------------------#
                #     complementary_addr    complementary_5n_addr
                return BasePairType.OVERHANG_ON_THIS_STRAND_3P
            elif adjacent_strand_type is _AdjacentDuplexType.TOP_RIGHT_OVERHANG:
                #                              # #
                #                              | |
                #               domain_3n_addr | | adjacent_5n_addr
                #                              | |
                #                              # #
                #             domain_addr      # #       adjacent_addr
                #    #-------------------------# #-------------------------------------#
                #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
                #    #-------------------------###-------------------------------------#
                #     complementary_addr    complementary_5n_addr
                return BasePairType.OVERHANG_ON_BOTH_STRANDS_3P
            elif adjacent_strand_type is _AdjacentDuplexType.TOP_RIGHT_BOUND_OVERHANG:
                # TODO: Possible case (nick n-arm junction)
                #                              #                    # #
                #                              |                    |-|
                #               domain_3n_addr |                    |-| adjacent_5n_addr
                #                              |                    |-|
                #                              #                    # #
                #             domain_addr      #                      #       adjacent_addr
                #    #-------------------------#                      #-------------------------------------#
                #     |||||||||||||||||||||||||                        |||||||||||||||||||||||||||||||||||||
                #    #-------------------------########################-------------------------------------#
                #     complementary_addr                         complementary_5n_addr
                return BasePairType.OTHER
            else:
                # Shouldn't reach here
                assert False
        else:
            # domain_addr's 3' neighbor is a bound domain

            # Techinically, could be an interior base, but we should have caught this earlier
            # back when we were determining AdjacentDuplexType.
            #
            # Assertion checks that it is not an internal base
            #
            #             domain_addr            domain_3n_addr == adjacent_addr
            #    #-------------------------###-------------------------------------#
            #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
            #    #-------------------------###-------------------------------------#
            #        complementary_addr              complementary_5n_addr
            assert domain_3n_addr != adjacent_addr

            # Declare new variables:
            domain_3n_complementary_addr = all_bound_domain_addresses[domain_3n_addr]
            domain_3n_complementary_3n_addr = domain_3n_complementary_addr.neighbor_3p()
            #             domain_addr                 domain_3n_addr
            #    #-------------------------###-------------------------------------#
            #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
            #    #-------------------------# #-------------------------------------#
            #        complementary_addr      # domain_3n_complementary_addr
            #                                #
            #                                |
            #                                | domain_3n_complementary_3n_addr
            #                                |
            #                                #

            # Three cases:
            #
            # domain_3n_complementary_addr is 3' end
            #             domain_addr                 domain_3n_addr
            #    #-------------------------###-------------------------------------#
            #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
            #    #-------------------------# <-------------------------------------#
            #         complementary_addr         domain_3n_complementary_addr
            #
            # domain_3n_complementary_3n_addr is unbound overhang
            #             domain_addr                 domain_3n_addr
            #    #-------------------------###-------------------------------------#
            #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
            #    #-------------------------# #-------------------------------------#
            #           complementary_addr   # domain_3n_complementary_addr
            #                                #
            #                                |
            #                                | domain_3n_complementary_3n_addr
            #                                |
            #                                #
            #
            # domain_3n_complementary_3n_addr is unbound overhang
            #             domain_addr                 domain_3n_addr
            #    #-------------------------###-------------------------------------#
            #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
            #    #-------------------------# #-------------------------------------#
            #         complementary_addr   ? #    domain_3n_complementary_addr
            #                              # #
            #                              |-|
            #                              |-| domain_3n_complementary_3n_addr
            #                              |-|
            #                              #-#
            #
            # Variable is None, False, True respectively based on cases above
            domain_3n_complementary_3n_addr_is_bound: Optional[bool] = None
            if domain_3n_complementary_3n_addr is not None:
                domain_3n_complementary_3n_addr_is_bound = domain_3n_complementary_3n_addr in all_bound_domain_addresses

            # Not an internal base pair since domain_addr's 3' neighbor is
            # bounded to a domain that is not complementary's 5' neighbor
            if adjacent_strand_type is _AdjacentDuplexType.BOTTOM_RIGHT_EMPTY:
                # NICK_5P
                #
                #             domain_addr                 domain_3n_addr
                #    #-------------------------###-------------------------------------#
                #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
                #    #-------------------------] <-------------------------------------#
                #        complementary_addr           domain_3n_complementary_addr
                #
                #                        OR
                #
                # OVERHANG_ON_ADJACENT_STRAND_5P
                #
                #             domain_addr                 domain_3n_addr
                #    #-------------------------###-------------------------------------#
                #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
                #    #-------------------------] #-------------------------------------#
                #          complementary_addr    #   domain_3n_complementary_addr
                #                                #
                #                                |
                #                                | domain_3n_complementary_3n_addr
                #                                |
                #                                #
                #
                #                        OR
                #
                # OTHER
                #
                #             domain_addr                 domain_3n_addr
                #    #-------------------------###-------------------------------------#
                #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
                #    #-------------------------] #-------------------------------------#
                #           complementary_addr   # domain_3n_complementary_addr
                #                              # #
                #                              |-|
                #                              |-| domain_3n_complementary_3n_addr
                #                              |-|
                #                              # #
                if domain_3n_complementary_3n_addr_is_bound is None:
                    return BasePairType.NICK_5P
                elif domain_3n_complementary_3n_addr_is_bound is False:
                    return BasePairType.OVERHANG_ON_ADJACENT_STRAND_5P
                else:
                    return BasePairType.OTHER
            elif adjacent_strand_type is _AdjacentDuplexType.BOTTOM_RIGHT_DANGLE:
                # OVERHANG_ON_THIS_STRAND_5P
                #
                #             domain_addr               domain_3n_addr
                #    #-------------------------###-------------------------------------#
                #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
                #    #-------------------------# #-------------------------------------#
                #          complementary_addr  #   domain_3n_complementary_addr
                #                              #
                #                              |
                #                              |
                #                              |
                #                              #
                #
                #                        OR
                #
                # OVERHANG_ON_BOTH_STRAND_5P
                #
                #             domain_addr               domain_3n_addr
                #    #-------------------------###-------------------------------------#
                #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
                #    #-------------------------# #-------------------------------------#
                #           complementary_addr # # domain_3n_complementary_addr
                #                              # #
                #                              | |
                #                              | | domain_3n_complementary_3n_addr
                #                              | |
                #                              # #
                #
                #                        OR
                #
                #
                # OTHER
                # TODO: Possible case (nick n-arm junction)
                #
                #             domain_addr                                   domain_3n_addr
                #    #-------------------------########-------------------------------------#
                #     |||||||||||||||||||||||||        |||||||||||||||||||||||||||||||||||||
                #    #-------------------------#      #-------------------------------------#
                #           complementary_addr #      # domain_3n_complementary_addr
                #                              #    # #
                #                              |    |-|
                #                              |    |-| domain_3n_complementary_3n_addr
                #                              |    |-|
                #                              #    # #

                if domain_3n_complementary_3n_addr_is_bound is None:
                    return BasePairType.OVERHANG_ON_THIS_STRAND_5P
                elif domain_3n_complementary_3n_addr_is_bound is False:
                    return BasePairType.OVERHANG_ON_BOTH_STRANDS_5P
                else:
                    return BasePairType.OTHER
            elif adjacent_strand_type is _AdjacentDuplexType.TOP_RIGHT_5P:
                # TODO: Possible case (nick n-arm junction)
                # TODO: Bound DANGLE_5P_3P? or OTHER?
                #                              # #
                #                              |-|
                #               domain_3n_addr |-| domain_3n_complementary_addr
                #                              |-|
                #                              # v
                #             domain_addr      #           adjacent_addr
                #    #-------------------------# [-------------------------------------#
                #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
                #    #-------------------------###-------------------------------------#
                #     complementary_addr                 complementary_5n_addr
                #
                #
                #
                #                              # #
                #                              |-|
                #               domain_3n_addr |-| domain_3n_complementary_addr
                #                              |-|
                #                              # ##---------#
                #             domain_addr      #       adjacent_addr
                #    #-------------------------# [-------------------------------------#
                #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
                #    #-------------------------###-------------------------------------#
                #     complementary_addr                                  complementary_5n_addr
                #
                #
                #
                #
                #                              # #
                #                              |-|
                #               domain_3n_addr |-| domain_3n_complementary_addr
                #                              |-|
                #                              # ##---------#
                #             domain_addr      #   |||||||||                     adjacent_addr
                #    #-------------------------#  #---------#         [-------------------------------------#
                #     |||||||||||||||||||||||||                        |||||||||||||||||||||||||||||||||||||
                #    #-------------------------########################-------------------------------------#
                #     complementary_addr                                     complementary_5n_addr
                return BasePairType.OTHER
            elif adjacent_strand_type is _AdjacentDuplexType.TOP_RIGHT_OVERHANG:
                # TODO: Possible case (nick n-arm junction)
                # Bound DANGLE_5P_3P?
                #                              # #                    #
                #                              |-|                    |
                #               domain_3n_addr |-|                    | adjacent_5n_addr
                #                              |-|                    |
                #                              # v                    #
                #             domain_addr      #                      #       adjacent_addr
                #    #-------------------------#                      #-------------------------------------#
                #     |||||||||||||||||||||||||                        |||||||||||||||||||||||||||||||||||||
                #    #-------------------------########################-------------------------------------#
                #     complementary_addr                         complementary_5n_addr
                #
                #
                #
                #                              # #                    #
                #                              |-|                    |
                #               domain_3n_addr |-|                    | adjacent_5n_addr
                #                              |-|                    |
                #                              # ##---------#         #
                #             domain_addr      #                      #       adjacent_addr
                #    #-------------------------#                      #-------------------------------------#
                #     |||||||||||||||||||||||||                        |||||||||||||||||||||||||||||||||||||
                #    #-------------------------########################-------------------------------------#
                #     complementary_addr                         complementary_5n_addr
                #
                #                              # #                    #
                #                              |-|                    |
                #               domain_3n_addr |-|                    | adjacent_5n_addr
                #                              |-|                    |
                #                              # ##---------#         #
                #             domain_addr      #   |||||||||          #       adjacent_addr
                #    #-------------------------#  #---------#         #-------------------------------------#
                #     |||||||||||||||||||||||||                        |||||||||||||||||||||||||||||||||||||
                #    #-------------------------########################-------------------------------------#
                #     complementary_addr                         complementary_5n_addr
                return BasePairType.OTHER
            elif adjacent_strand_type is _AdjacentDuplexType.TOP_RIGHT_BOUND_OVERHANG:
                #                              # #                  # #
                #                              |-|                  |-|
                #               domain_3n_addr |-|                  |-| adjacent_5n_addr
                #                              |-|                  |-|
                #                              # #                  # #
                #             domain_addr      #                      #       adjacent_addr
                #    #-------------------------#                      #-------------------------------------#
                #     |||||||||||||||||||||||||                        |||||||||||||||||||||||||||||||||||||
                #    #-------------------------########################-------------------------------------#
                #     complementary_addr                         complementary_5n_addr
                assert adjacent_5n_addr is not None
                if domain_3n_addr == all_bound_domain_addresses[adjacent_5n_addr]:
                    #                              # #
                    #                              |-|
                    #               domain_3n_addr |-| adjacent_5n_addr
                    #                              |-|
                    #                              # #
                    #             domain_addr      # #       adjacent_addr
                    #    #-------------------------# #-------------------------------------#
                    #     |||||||||||||||||||||||||   |||||||||||||||||||||||||||||||||||||
                    #    #-------------------------###-------------------------------------#
                    #     complementary_addr    complementary_5n_addr
                    return BasePairType.THREE_ARM_JUNCTION
                else:
                    # Could possibly be n-arm junction
                    return BasePairType.OTHER
            else:
                # Should not make it here
                assert False


@dataclass(frozen=True)
class _BasePairDomainEndpoint:
    """A base pair endpoint in the context of the domain it resides on.

    Numbering the bases in strand complex order, of the two bound domains,
    domain1 is defined to be the domain that occurs earlier and domain2 is
    defined to be the domain that occurs later.

    ```(this line is to avoid Python syntax highlighting of ASCII art below)
                 domain1_5p_index
                 |
    domain1   5' --------------------------------- 3'
                 | | | | | | | | | | | | | | | | |
    domain2   3' --------------------------------- 5'
              ^  |                                 ^
              |  domain2_3p_index                  |
              |                                    |
              |                                    |
    domain1_5p_domain2_3p_exterior_base_pair_type  |
                                                   |
                            domain1_3p_domain2_5p_exterior_base_pair_type
    ```
    """
    domain1_5p_index: int
    domain2_3p_index: int
    domain_base_length: int
    domain1_5p_domain2_base_pair_type: BasePairType
    domain1_3p_domain1_base_pair_type: BasePairType


@dataclass(frozen=True)
class _BasePair:
    base_index1: int
    base_index2: int
    base_pairing_probability: float
    base_pair_type: BasePairType


BaseAddress = Union[int, Tuple[StrandDomainAddress, int]]
"""Represents a reference to a base. Can be either specified as a NUPACK base
index or an index of a dsd :py:class:`StrandDomainAddress`:
"""
BasePairAddress = Tuple[BaseAddress, BaseAddress]
"""Represents a reference to a base pair
"""
BoundDomains = Tuple[StrandDomainAddress, StrandDomainAddress]
"""Represents bound domains
"""


def _get_implicitly_bound_domain_addresses(strand_complex: Complex,
                                           nonimplicit_base_pairs_domain_names: Optional[Set[str]] = None) \
        -> Dict[StrandDomainAddress, StrandDomainAddress]:
    """Returns a map of all the implicitly bound domain addresses

    :param strand_complex: Tuple of strands representing strand complex
    :type strand_complex: Complex
    :param nonimplicit_base_pairs_domain_names: Set of all domain names to ignore in this search, defaults to None
    :type nonimplicit_base_pairs_domain_names: Set[str], optional
    :return: Map of all implicitly bound domain addresses
    :rtype: Dict[StrandDomainAddress, StrandDomainAddress]
    """
    if nonimplicit_base_pairs_domain_names is None:
        nonimplicit_base_pairs_domain_names = set()

    implicitly_bound_domain_addresses = {}
    implicit_seen_domains: Dict[str, StrandDomainAddress] = {}
    for strand in strand_complex:
        for domain_idx, domain in enumerate(strand.domains):
            # Get domain_name
            domain_name = domain.name
            if domain_idx in strand.starred_domain_indices:
                domain_name = domain.starred_name

            # Move on to next domain if it was paired via nonimplicit_base_pairs
            if domain_name in nonimplicit_base_pairs_domain_names:
                continue

            # populate implicit bounded_domains
            strand_domain_address = StrandDomainAddress(strand, domain_idx)
            # Assertions checks that domain_name was not previously seen.
            # This is to check that the non-competition requirement on
            # implicit domains was properly checked earlier in input validation.
            assert domain_name not in implicit_seen_domains
            implicit_seen_domains[domain_name] = strand_domain_address

            complementary_domain_name = Domain.complementary_domain_name(domain_name)
            if complementary_domain_name in implicit_seen_domains:
                complementary_strand_domain_address = implicit_seen_domains[complementary_domain_name]
                implicitly_bound_domain_addresses[strand_domain_address] = complementary_strand_domain_address
                implicitly_bound_domain_addresses[complementary_strand_domain_address] = strand_domain_address

    return implicitly_bound_domain_addresses


def _get_addr_to_starting_base_pair_idx(strand_complex: Complex) -> Dict[StrandDomainAddress, int]:
    """Returns a mapping between StrandDomainAddress and the base index of the
    5' end base of the domain

    :param strand_complex: Tuple of strands representing strand complex
    :type strand_complex: Complex
    :return: Map of StrandDomainAddress to starting base index
    :rtype: Dict[StrandDomainAddress, int]
    """
    # Fill addr_to_starting_base_pair_idx and all_bound_domain_addresses
    addr_to_starting_base_pair_idx = {}
    domain_base_index = 0
    for strand in strand_complex:
        for domain_idx, domain in enumerate(strand.domains):
            addr_to_starting_base_pair_idx[StrandDomainAddress(strand, domain_idx)] = domain_base_index
            domain_base_index += domain.length

    return addr_to_starting_base_pair_idx


def _leafify_domain(domain: Domain) -> List[Domain]:
    """Returns the list of all leaf subdomains that make up domain

    :param domain: Domain
    :type domain: Domain
    :return: List of leaf subdomains
    :rtype: List[Domain]
    """
    if len(domain.subdomains) == 0:
        return [domain]
    else:
        ret = []
        for sd in domain.subdomains:
            ret += _leafify_domain(sd)
        return ret


def _leafify_strand(
        strand: Strand,
        addr_translation_table: Dict[StrandDomainAddress, List[StrandDomainAddress]]) -> Strand:
    """Create a new strand that is made of the leaf subdomains. Also updates an
    addr_translation_table which maps StrandDomainAddress from old strand to new
    strand. Since a domain may consist of multiple subdomains, a single StrandDomainAddress
    may map to a list of StrandDomainAddresses, listed in 5' to 3' order.

    :param strand: Strand
    :type strand: Strand
    :param addr_translation_table: Maps old StrandDomainAddress to new StrandDomainAddress
    :type addr_translation_table: Dict[StrandDomainAddress, List[StrandDomainAddress]]
    :return: Leafified strand
    :rtype: Strand
    """
    leafify_domains: List[List[Domain]] = [_leafify_domain(d) for d in strand.domains]
    new_domains: List[Domain] = []
    new_starred_domain_indices: List[int] = []
    new_starred_domain_idx = 0
    addr_translation_table_without_strand: Dict[int, List[int]] = {}
    for (idx, leaf_domain_list) in enumerate(leafify_domains):
        new_domain_indices = []
        for i in range(new_starred_domain_idx, new_starred_domain_idx + len(leaf_domain_list)):
            new_domain_indices.append(i)

        addr_translation_table_without_strand[idx] = new_domain_indices
        if idx in strand.starred_domain_indices:
            new_domains.extend(reversed(leaf_domain_list))
            # Star every single subdomain that made up original starred domain
            new_starred_domain_indices.extend(new_domain_indices)
        else:
            new_domains.extend(leaf_domain_list)

        new_starred_domain_idx += len(leaf_domain_list)
    new_strand: Strand = Strand(domains=new_domains, starred_domain_indices=new_starred_domain_indices,
                                name=f"leafifed {strand.name}")
    for idx, new_idxs in addr_translation_table_without_strand.items():
        new_addrs = [StrandDomainAddress(new_strand, new_idx) for new_idx in new_idxs]
        addr_translation_table[StrandDomainAddress(strand, idx)] = new_addrs

    new_strand.compute_derived_fields()

    return new_strand


def _get_base_pair_domain_endpoints_to_check(
        strand_complex: Complex,
        nonimplicit_base_pairs: Iterable[BoundDomains] = None) -> Set[_BasePairDomainEndpoint]:
    """Returns the set of all the _BasePairDomainEndpoint to check

    :param strand_complex: Tuple of strands representing strand complex
    :type strand_complex: Complex
    :param nonimplicit_base_pairs: Set of base pairs that cannot be inferred (usually due to competition), defaults to None
    :type nonimplicit_base_pairs: Iterable[BoundDomains], optional
    :raises ValueError: If there are multiple instances of the same strand in a complex
    :raises ValueError: If competitive domains are not specificed in nonimplicit_base_pairs
    :raises ValueError: If address given in nonimplicit_base_pairs is not found
    :return: Set of all the _BasePairDomainEndpoint to check
    :rtype: Set[_BasePairDomainEndpoint]
    """
    addr_translation_table: Dict[StrandDomainAddress, List[StrandDomainAddress]] = {}

    # Need to convert strands into strands lowest level subdomains
    leafify_strand_complex = tuple(
        [_leafify_strand(strand, addr_translation_table) for strand in strand_complex])

    new_nonimplicit_base_pairs = []
    if nonimplicit_base_pairs:
        for bp in nonimplicit_base_pairs:
            (addr1, addr2) = bp
            new_addr1_list = addr_translation_table[addr1]
            new_addr2_list = list(reversed(addr_translation_table[addr2]))

            assert len(new_addr1_list) == len(new_addr2_list)
            for idx in range(len(new_addr1_list)):
                new_nonimplicit_base_pairs.append((new_addr1_list[idx], new_addr2_list[idx]))

    return __get_base_pair_domain_endpoints_to_check(
        leafify_strand_complex, nonimplicit_base_pairs=new_nonimplicit_base_pairs)


def __get_base_pair_domain_endpoints_to_check(
        strand_complex: Complex,
        nonimplicit_base_pairs: Iterable[BoundDomains] = None) -> Set[_BasePairDomainEndpoint]:
    """Returns the set of all the _BasePairDomainEndpoint to check

    :param strand_complex: Tuple of strands representing strand complex
    :type strand_complex: Complex
    :param nonimplicit_base_pairs: Set of base pairs that cannot be inferred (usually due to competition), defaults to None
    :type nonimplicit_base_pairs: Iterable[BoundDomains], optional
    :raises ValueError: If there are multiple instances of the same strand in a complex
    :raises ValueError: If competitive domains are not specificed in nonimplicit_base_pairs
    :raises ValueError: If address given in nonimplicit_base_pairs is not found
    :return: Set of all the _BasePairDomainEndpoint to check
    :rtype: Set[_BasePairDomainEndpoint]
    """
    # Maps domain pairs
    all_bound_domain_addresses: Dict[StrandDomainAddress, StrandDomainAddress] = {}

    # Keep track of all the domain names that are provided as
    # part of a nonimplicit_base_pair so that input validation
    # knows to ignore these domain names.
    nonimplicit_base_pairs_domain_names: Set[str] = set()

    if nonimplicit_base_pairs is not None:
        for (addr1, addr2) in nonimplicit_base_pairs:
            d1 = addr1.strand.domains[addr1.domain_idx]
            d2 = addr2.strand.domains[addr2.domain_idx]
            if d1 is not d2:
                print('WARNING: a base pair is specified between two different domain objects')
            nonimplicit_base_pairs_domain_names.add(d1.get_name(starred=False))
            nonimplicit_base_pairs_domain_names.add(d1.get_name(starred=True))
            nonimplicit_base_pairs_domain_names.add(d2.get_name(starred=False))
            nonimplicit_base_pairs_domain_names.add(d2.get_name(starred=True))

            all_bound_domain_addresses[addr1] = addr2
            all_bound_domain_addresses[addr2] = addr1

    # Input validation checks:
    #
    # No repeated strand
    #
    # No competition:
    #   check no "competition" between domain (at most one
    #   starred domain for every domain, unless given as nonimplicit_base_pair)
    #   Count number of occuruences of each domain
    seen_strands: Set[Strand] = set()
    domain_counts: Dict[str, int] = defaultdict(int)
    for strand in strand_complex:
        if strand in seen_strands:
            raise ValueError(f"Multiple instances of a strand in a complex is not allowed."
                             " Please make a separate Strand object with the same Domain objects in the same order"
                             " but a different strand name")
        seen_strands.add(strand)
        for idx, domain in enumerate(strand.domains):
            is_starred = idx in strand.starred_domain_indices
            domain_name = domain.get_name(is_starred)
            if domain_name not in nonimplicit_base_pairs_domain_names:
                domain_counts[domain_name] += 1

    # Check final counts of each domain for competition
    for domain_name in domain_counts:
        domain_name_complement = Domain.complementary_domain_name(domain_name)
        if domain_name_complement in domain_counts and domain_counts[domain_name_complement] > 1:
            assert domain_name not in nonimplicit_base_pairs_domain_names
            raise ValueError(
                f"Multiple instances of domain in a complex is not allowed when its complement is also in the complex. "
                f"Violating domain: {domain_name_complement}")
    # End Input Validation #

    addr_to_starting_base_pair_idx: Dict[StrandDomainAddress,
                                         int] = _get_addr_to_starting_base_pair_idx(strand_complex)
    all_bound_domain_addresses.update(_get_implicitly_bound_domain_addresses(
        strand_complex, nonimplicit_base_pairs_domain_names))

    # Set of all bound domain endpoints to check.
    base_pair_domain_endpoints_to_check: Set[_BasePairDomainEndpoint] = set()

    for (domain_addr, comple_addr) in all_bound_domain_addresses.items():
        domain_base_length = domain_addr.domain().length
        assert domain_base_length == comple_addr.domain().length

        if domain_addr not in addr_to_starting_base_pair_idx:
            if domain_addr.domain().name in nonimplicit_base_pairs_domain_names:
                raise ValueError(f'StrandDomainAddress {domain_addr} is not found in given complex')
            else:
                print(f'StrandDomainAddress {domain_addr} is not found in given complex')
                assert False

        if comple_addr not in addr_to_starting_base_pair_idx:
            if comple_addr.domain().name in nonimplicit_base_pairs_domain_names:
                raise ValueError(f'StrandDomainAddress {comple_addr} is not found in given complex')
            else:
                print(f'StrandDomainAddress {comple_addr} is not found in given complex')
                assert False

        domain_5p = addr_to_starting_base_pair_idx[domain_addr]
        comple_5p = addr_to_starting_base_pair_idx[comple_addr]

        # Define domain1 to be the "earlier" domain
        if domain_5p < comple_5p:
            domain1_addr = domain_addr
            domain1_5p = domain_5p

            domain2_addr = comple_addr
            domain2_5p = comple_5p
        else:
            domain1_addr = comple_addr
            domain1_5p = comple_5p

            domain2_addr = domain_addr
            domain2_5p = domain_5p

        domain2_3p = domain2_5p + domain_base_length - 1

        # domain1                     5' --------------------------------- 3'
        #                                | | | | | | | | | | | | | | | | |
        # domain2                     3' --------------------------------- 5'
        #                             ^                                    ^
        #                             |                                    |
        #                   d1_5p_d2_3p_ext_bp_type                        |
        #                                                                  |
        #                                                       d1_3p_d2_5p_ext_bp_type
        d1_3p_d2_5p_ext_bp_type = _exterior_base_type_of_domain_3p_end(domain1_addr,
                                                                       all_bound_domain_addresses)
        d1_5p_d2_3p_ext_bp_type = _exterior_base_type_of_domain_3p_end(domain2_addr,
                                                                       all_bound_domain_addresses)

        base_pair_domain_endpoints_to_check.add(_BasePairDomainEndpoint(
            domain1_5p, domain2_3p, domain_base_length, d1_5p_d2_3p_ext_bp_type, d1_3p_d2_5p_ext_bp_type))

    return base_pair_domain_endpoints_to_check


def nupack_complex_secondary_structure_constraint(
        strand_complexes: List[Complex],
        nonimplicit_base_pairs: Optional[Iterable[BoundDomains]] = None,
        all_base_pairs: Optional[Iterable[BoundDomains]] = None,
        base_pair_prob_by_type: Optional[Dict[BasePairType, float]] = None,
        base_pair_prob_by_type_upper_bound: Dict[BasePairType, float] = None,
        base_pair_prob: Dict[BasePairAddress, float] = None,
        base_unpaired_prob: Dict[BaseAddress, float] = None,
        base_pair_prob_upper_bound: Dict[BasePairAddress, float] = None,
        base_unpaired_prob_upper_bound: Dict[BaseAddress, float] = None,
        temperature: float = dv.default_temperature,
        sodium: float = dv.default_sodium,
        magnesium: float = dv.default_magnesium,
        weight: float = 1.0,
        score_transfer_function: Callable[[float], float] = default_score_transfer_function,
        description: Optional[str] = None,
        short_description: str = 'complex_secondary_structure_nupack',
        threaded: bool = False,
) -> ComplexConstraint:
    """Returns constraint that checks given base pairs probabilities in tuples of :any:`Strand`'s

    :param strand_complexes:
        Iterable of :any:`Strand` tuples
    :type strand_complexes:
        List[Complex]
    :param nonimplicit_base_pairs:
        List of nonimplicit base pairs that cannot be inferred because multiple
        instances of the same :py:class:`Domain` exist in complex.

        The :py:attr:`StrandDomainAddress.strand` field of each address should
        reference a strand in the first complex in ``strand_complexes``.

        For example,
        if one :py:class:`Strand` has one T :py:class:`Domain` and another
        strand in the complex has two T* :py:class:`Domain` s, then the intended
        binding graph cannot be inferred and must be stated explicitly in this
        field.
    :type nonimplicit_base_pairs:
        Optional[Iterable[BoundDomains]]
    :param all_base_pairs:
        List of all base pairs in complex. If not provided, then base pairs are
        infered based on the name of :py:class:`Domain` s in the complex as well
        as base pairs specified in ``nonimplicit_base_pairs``.


        **TODO**: This has not been implemented yet, and the behavior is as if this
        parameter is always ``None`` (binding graph is always inferred).
    :type all_base_pairs:
        Optional[Iterable[BoundDomains]]
    :param base_pair_prob_by_type:
        Probability lower bounds for each :py:class:`BasePairType`.
        All :py:class:`BasePairType` comes with a default
        such as :py:data:`default_interior_to_strand_probability` which will be
        used if a lower bound is not specified for a particular type.

        **Note**: Despite the name of this parameter, set thresholds for unpaired
        bases by specifying a threshold for :py:attr:`BasePairType.UNPAIRED`.
    :type base_pair_prob_by_type:
        Optional[Dict[BasePairType, float]]
    :param base_pair_prob_by_type_upper_bound:
        Probability upper bounds for each :py:class:`BasePairType`.
        By default, no upper bound is set.

        **Note**: Despite the name of this parameter, set thresholds for unpaired
        bases by specifying a threshold for :py:attr:`BasePairType.UNPAIRED`.

        **TODO**: This has not been implemented yet.
    :type base_pair_prob_by_type_upper_bound:
        Dict[BasePairType, float], optional
    :param base_pair_prob:
        Probability lower bounds for each :py:class:`BasePairAddress` which takes
        precedence over probabilities specified by ``base_pair_prob_by_type``.

        **TODO**: This has not been implemented yet.
    :type base_pair_prob:
        Optional[Dict[BasePairAddress, float]]
    :param base_unpaired_prob:
        Probability lower bounds for each :py:class:`BaseAddress` representing
        unpaired bases. These lower bounds take precedence over the probability
        specified by ``base_pair_prob_by_type[BasePairType.UNPAIRED]``.
    :type base_unpaired_prob:
        Optional[Dict[BaseAddress, float]]
    :param base_pair_prob_upper_bound:
        Probability upper bounds for each :py:class`BasePairAddress` which takes
        precedence over probabilties specified by ``base_pair_prob_by_type_upper_bound``.
    :type base_pair_prob_upper_bound:
        Optional[Dict[BasePairAddress, float]]
    :param base_unpaired_prob_upper_bound:
        Probability upper bounds for each :py:class:`BaseAddress` representing
        unpaired bases. These lower bounds take precedence over the probability
        specified by ``base_pair_prob_by_type_upper_bound[BasePairType.UNPAIRED]``.
    :type base_unpaired_prob_upper_bound:
        Optional[Dict[BaseAddress, float]]
    :param temperature:
        Temperature specified in °C, defaults to :py:data:`vienna_nupack.default_temperature`.
    :type temperature: float, optional
    :param sodium:
        molarity of sodium (more generally, monovalent ions such as Na+, K+, NH4+)
        in moles per liter
    :param magnesium:
        molarity of magnesium (Mg++) in moles per liter
    :param weight:
        See :py:data:`Constraint.weight`, defaults to 1.0
    :type weight:
        float, optional
    :param score_transfer_function:
        Score transfer function to use. By default, f(x) = x**2 is used, where x
        is the sum of the squared errors of each base pair that violates the
        threshold.
    :type score_transfer_function: Callable[[float], float], optional
    :param description:
        See :py:data:`Constraint.description`, defaults to None
    :type description:
        Optional[str], optional
    :param short_description:
        See :py:data:`Constraint.short_description` defaults to 'complex_secondary_structure_nupack'
    :type short_description:
        str, optional
    :param threaded:
        **TODO**: Implement this
    :type threaded:
        bool, optional
    :raises ImportError:
        If NUPACK 4 is not installed.
    :raises ValueError:
        If ``strand_complexes`` is not valid. In order for ``strand_complexes`` to
        be valid, ``strand_complexes`` must:

        * Consist of complexes (tuples of :py:class:`Strand` objects)
        * Each complex must be of the same motif

            * Same number of :py:class:`Strand` s in each complex
            * Same number of :py:class:`Domain` s in each :py:class:`Strand`
            * Same number of bases in each :py:class:`Domain`
    :return: ComplexConstraint
    :rtype: ComplexConstraint
    """
    try:
        from nupack import Complex as NupackComplex
        from nupack import Model as NupackModel
        from nupack import ComplexSet as NupackComplexSet
        from nupack import Strand as NupackStrand
        from nupack import SetSpec as NupackSetSpec
        from nupack import complex_analysis as nupack_complex_analysis
        from nupack import PairsMatrix as NupackPairsMatrix
    except ModuleNotFoundError:
        raise ImportError(
            'NUPACK 4 must be installed to use nupack_4_complex_secondary_structure_constraint. '
            'Installation instructions can be found at https://piercelab-caltech.github.io/nupack-docs/start/.')

    # Start Input Validation
    if len(strand_complexes) == 0:
        raise ValueError("strand_complexes list cannot be empty.")

    strand_complex_template = strand_complexes[0]

    if type(strand_complex_template) is not tuple:
        raise ValueError(
            f"First element in strand_complexes was not a tuple of Strands. "
            f"Please provide a tuple of Strands.")

    for strand in strand_complex_template:
        if type(strand) is not Strand:
            raise ValueError(f"Complex at index 0 contained non-Strand object: {type(strand)}")

    for strand_complex in strand_complexes:
        for strand in strand_complex:
            for domain in strand.domains:
                if not domain.has_length():
                    raise ValueError(f'''\
Domain {domain.name} has no length yet. To use 
nupack_complex_secondary_structure_constraint, each Domain must have a length 
assigned, either by assigning it a DomainPool first, or by setting the Domain 
to have a fixed DNA sequence by calling domain.set_fixed_sequence.''')

    for idx in range(1, len(strand_complexes)):
        strand_complex = strand_complexes[idx]
        if type(strand_complex) is not tuple:
            raise ValueError(
                f"Element at index {idx} was not a tuple of Strands. Please provide a tuple of Strands.")
        if len(strand_complex) != len(strand_complex_template):
            raise ValueError(
                f"Inconsistent complex structures: Complex at index {idx} contained {len(strand_complex)} "
                f"strands, but complex at index 0 contained {len(strand_complex_template)} strands.")
        for s in range(len(strand_complex)):
            other_strand: Strand = strand_complex[s]
            template_strand: Strand = strand_complex_template[s]
            if type(other_strand) is not Strand:
                raise ValueError(
                    f"Complex at index {idx} contained non-Strand object at index {s}: {type(other_strand)}")
            if len(other_strand.domains) != len(template_strand.domains):
                raise ValueError(
                    f"Strand {other_strand} (index {s} of strand_complexes at index {idx}) does not match "
                    f"the provided template ({template_strand}). "
                    f"Strand {other_strand} contains {len(other_strand.domains)} domains but template "
                    f"strand {template_strand} contains {len(template_strand.domains)} domains.")
            for d in range(1, len(other_strand.domains)):
                domain_length: int = other_strand.domains[d].length
                template_domain_length: int = template_strand.domains[d].length
                if domain_length != template_domain_length:
                    raise ValueError(
                        f"Strand {other_strand} (the strand at index {s} of the complex located at index "
                        f"{idx} of strand_complexes) does not match the "
                        f"provided template ({template_strand}): domain at index {d} is length "
                        f"{domain_length}, but expected {template_domain_length}.")

    base_pair_domain_endpoints_to_check = _get_base_pair_domain_endpoints_to_check(
        strand_complex_template, nonimplicit_base_pairs=nonimplicit_base_pairs)

    # Start populating base_pair_probs
    base_type_probability_threshold: Dict[BasePairType, float] = (
        {} if base_pair_prob_by_type is None else base_pair_prob_by_type.copy())
    for base_type in BasePairType:
        if base_type not in base_type_probability_threshold:
            base_type_probability_threshold[base_type] = base_type.default_pair_probability()
    # End populating base_pair_probs

    if description is None:
        description = 'Base pair probability of complex'

    def evaluate(strand_complex_: Complex) -> float:
        bps = _violation_base_pairs(strand_complex_)
        err_sq = 0.0
        for bp in bps:
            e = base_type_probability_threshold[bp.base_pair_type] - bp.base_pairing_probability
            assert e > 0
            err_sq += e ** 2
        return err_sq

    # summary would print all the base pairs
    # * indices of the bases e.g 2,7: 97.3% (<99%);  9,13: 75% (<80%); 1,7: 2.1% (>1%)
    # * maybe consider puting second and after base pairs on new line with indent
    def summary(strand_complex_: Complex) -> str:
        bps = _violation_base_pairs(strand_complex_)
        if len(bps) == 0:
            return "\tAll base pairs satisfy thresholds."
        summary_list = []
        for bp in bps:
            i = bp.base_index1
            j = bp.base_index2
            p = bp.base_pairing_probability
            t = bp.base_pair_type
            summary_list.append(
                f'\t{i},{j}: {math.floor(100 * p)}% (<{round(100 * base_type_probability_threshold[t])}% [{t}])')
        return '\n'.join(summary_list)

    def _violation_base_pairs(strand_complex_: Complex) -> List[_BasePair]:
        nupack_complex_result = dv.nupack_complex_base_pair_probabilities(strand_complex_,
                                                                          temperature=temperature,
                                                                          sodium=sodium, magnesium=magnesium)

        # DEBUG: Print out result matrix
        # for r in nupack_complex_result:
        #     for c in r:
        #         print("{:.2f}".format(c), end=' ')
        #     print()

        # DEBUG: Print out complex strands and sequences
        # for strand in strand_complex:
        #     print(f'{strand.name}: {strand.sequence()}')

        # Refactor all this into a function that returns all the base pairs that are below threshold
        # eval would take the squared sum of prob differences

        # Probability threshold
        internal_base_pair_prob = base_type_probability_threshold[BasePairType.INTERIOR_TO_STRAND]
        unpaired_base_prob = base_type_probability_threshold[BasePairType.UNPAIRED]
        border_internal_base_pair_prob = base_type_probability_threshold[
            BasePairType.ADJACENT_TO_EXTERIOR_BASE_PAIR]

        # Tracks which bases are paired. Used to determine unpaired bases.
        expected_paired_idxs: Set[int] = set()

        # Collect violating base pairs
        bps: List[_BasePair] = []
        for e in base_pair_domain_endpoints_to_check:
            domain1_5p = e.domain1_5p_index
            domain2_3p = e.domain2_3p_index
            domain_length_ = e.domain_base_length
            d1_5p_d2_3p_ext_bp_type = e.domain1_5p_domain2_base_pair_type
            d1_3p_d2_5p_ext_bp_type = e.domain1_3p_domain1_base_pair_type

            # Checks if base pairs at ends of domain to be above 40% probability
            domain1_3p = domain1_5p + (domain_length_ - 1)
            domain2_5p = domain2_3p - (domain_length_ - 1)

            d1_5p_d2_3p_ext_bp_prob_thres = base_type_probability_threshold[d1_5p_d2_3p_ext_bp_type]
            if nupack_complex_result[domain1_5p][domain2_3p] < d1_5p_d2_3p_ext_bp_prob_thres:
                bps.append(
                    _BasePair(
                        domain1_5p, domain2_3p, nupack_complex_result[domain1_5p][domain2_3p],
                        d1_5p_d2_3p_ext_bp_type))
            expected_paired_idxs.add(domain1_5p)
            expected_paired_idxs.add(domain2_3p)

            d1_3p_d2_5p_ext_bp_prob_thres = base_type_probability_threshold[d1_3p_d2_5p_ext_bp_type]
            if nupack_complex_result[domain1_3p][domain2_5p] < d1_3p_d2_5p_ext_bp_prob_thres:
                bps.append(
                    _BasePair(
                        domain1_3p, domain2_5p, nupack_complex_result[domain1_3p][domain2_5p],
                        d1_3p_d2_5p_ext_bp_type))
            expected_paired_idxs.add(domain1_3p)
            expected_paired_idxs.add(domain2_5p)
            # Check if base pairs interior to domain (note ascending base pair indices
            # for domain1 and descending base pair indices for domain2)
            #
            # Ex:
            #     0123
            #    [AGCT>    domain1
            #          \
            #          |
            #          /
            #    <TCGA]    domain2
            #     7654
            #
            # TODO: Rewrite this loop using numpy
            # domain1_idxs = np.arange(domain1_5p + 1, domain1_5p + domain_length - 1)
            # domain2_idxs = np.arange(domain2_3p - 1, ,-1)
            for i in range(1, domain_length_ - 1):
                row = domain1_5p + i
                col = domain2_3p - i

                # Determine if base pair is adjacent to exterior base pair
                prob_thres = internal_base_pair_prob
                bp_type = BasePairType.INTERIOR_TO_STRAND
                if i == 1 and d1_5p_d2_3p_ext_bp_type is not BasePairType.INTERIOR_TO_STRAND or i == domain_length_ - 2 and d1_3p_d2_5p_ext_bp_prob_thres is not BasePairType.INTERIOR_TO_STRAND:
                    prob_thres = border_internal_base_pair_prob
                    bp_type = BasePairType.ADJACENT_TO_EXTERIOR_BASE_PAIR

                if nupack_complex_result[row][col] < prob_thres:
                    bps.append(_BasePair(row, col, nupack_complex_result[row][col], bp_type))
                expected_paired_idxs.add(row)
                expected_paired_idxs.add(col)

        # Check base pairs that should not be paired are high probability
        for i in range(len(nupack_complex_result)):
            if i not in expected_paired_idxs and nupack_complex_result[i][i] < unpaired_base_prob:
                bps.append(_BasePair(i, i, nupack_complex_result[i][i], BasePairType.UNPAIRED))

        return bps

    return ComplexConstraint(description=description,
                             short_description=short_description,
                             weight=weight,
                             score_transfer_function=score_transfer_function,
                             threaded=threaded,
                             complexes=tuple(strand_complexes),
                             evaluate=evaluate,
                             summary=summary)
