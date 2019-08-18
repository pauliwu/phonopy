# Copyright (C) 2018 Atsushi Togo
# All rights reserved.
#
# This file is part of phonopy.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# * Redistributions of source code must retain the above copyright
#   notice, this list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright
#   notice, this list of conditions and the following disclaimer in
#   the documentation and/or other materials provided with the
#   distribution.
#
# * Neither the name of the phonopy project nor the names of its
#   contributors may be used to endorse or promote products derived
#   from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import numpy as np
from phonopy.harmonic.dynamical_matrix import get_dynamical_matrix
from phonopy.harmonic.dynmat_to_fc import (
    get_commensurate_points_in_integers, DynmatToForceConstants)
from phonopy.units import VaspToTHz, THzToEv, Kb, Hbar, AMU, EV, Angstrom, THz


class RandomDisplacements(object):
    """Generate random displacements by Canonical ensenmble.

    Note
    ----
    Phonon frequencies are used to calculate phonon occupation number,
    for which phonon frequencies have to be given in THz. Therefore unit
    conversion factor has to be specified at the initialization.

    Imaginary phonon modes are treated so as to have their absolute phonon
    frequencies |omega| and phonon modes having |omega| < cutoff_frequency
    are ignored.

    Attributes
    ----------
    u : ndarray
        Random atomic displacements generated by canonical distribution of
        harmonic oscillator. The unit of distance is Angstrom.
        shape=(number_of_snapshots, supercell_atoms, 3)
        dtype='double', order='C'

    """

    def __init__(self,
                 supercell,
                 primitive,
                 force_constants,
                 cutoff_frequency=None,
                 factor=VaspToTHz):
        """

        Parameters
        ----------
        supercell : Supercell
            Supercell.
        primitive : Primitive
            Primitive cell
        force_constants : array_like
            Force constants matrix. See the details at docstring of
            DynamialMatrix.
        cutoff_frequency : float
            Lowest phonon frequency below which frequency the phonon mode
            is treated specially. See _get_sigma. Default is None, which
            means 0.01.
        factor : float
            Phonon frequency unit conversion factor to THz

        """

        # Dynamical matrix without NAC because of commensurate points only
        self._dynmat = get_dynamical_matrix(
            force_constants, supercell, primitive)
        if cutoff_frequency is None or cutoff_frequency < 0:
            self._cutoff_frequency = 0.01
        else:
            self._cutoff_frequency = cutoff_frequency
        self._factor = factor
        self._T = None
        self.u = None

        self._unit_conversion = (Hbar * EV / AMU / THz
                                 / (2 * np.pi) / Angstrom ** 2)

        slat = supercell.cell
        self._rec_lat = np.linalg.inv(primitive.cell)
        smat = np.rint(np.dot(slat, self._rec_lat).T).astype(int)
        self._comm_points = get_commensurate_points_in_integers(smat)
        self._ii, self._ij = self._categorize_points()
        assert len(self._ii) + len(self._ij) * 2 == len(self._comm_points)

        s2p = primitive.s2p_map
        p2p = primitive.p2p_map
        self._s2pp = [p2p[i] for i in s2p]

        self._eigvals_ii = []
        self._eigvecs_ii = []
        self._phase_ii = []
        self._eigvals_ij = []
        self._eigvecs_ij = []
        self._phase_ij = []
        self._prepare()

        # This is set when running run_d2f.
        # The aim is to produce force constants from modified frequencies.
        self._force_constants = None

    def run(self, T, number_of_snapshots=1, random_seed=None, randn=None):
        """

        Parameters
        ----------
        T : float
            Temperature in Kelvin.
        number_of_snapshots : int
            Number of snapshots to be generated.
        random_seed : int or None, optional
            Random seed passed to np.random.seed. Default is None. Integer
            number has to be positive.
        randn : tuple
            (randn_ii, randn_ij).
            Used for testing purpose for the fixed random numbers of
            np.random.normal that can depends on system.

        """

        np.random.seed(seed=random_seed)

        N = len(self._comm_points)

        if randn is None:
            u_ii = self._solve_ii(T, number_of_snapshots)
            u_ij = self._solve_ij(T, number_of_snapshots)
        else:
            u_ii = self._solve_ii(T, number_of_snapshots, randn=randn[0])
            u_ij = self._solve_ij(T, number_of_snapshots, randn=randn[1])
        mass = self._dynmat.supercell.masses.reshape(-1, 1)
        u = np.array((u_ii + u_ij) / np.sqrt(mass * N),
                     dtype='double', order='C')
        self.u = u

    @property
    def frequencies(self):
        eigvals = np.vstack((self._eigvals_ii, self._eigvals_ij))
        freqs = np.sqrt(np.abs(eigvals)) * np.sign(eigvals) * self._factor
        return np.array(freqs, dtype='double', order='C')

    @frequencies.setter
    def frequencies(self, freqs):
        eigvals = (freqs / self._factor) ** 2
        if len(eigvals) != len(self._eigvals_ii) + len(self._eigvals_ij):
            raise RuntimeError("Dimension of frequencies is wrong.")

        self._eigvals_ii = eigvals[:len(self._eigvals_ii)]
        self._eigvals_ij = eigvals[len(self._eigvals_ii):]

    @property
    def qpoints(self):
        N = len(self._comm_points)
        return self._comm_points[self._ii + self._ij] / float(N)

    @property
    def force_constants(self):
        return self._force_constants

    def run_d2f(self):
        eigvals = np.vstack(
            (self._eigvals_ii, self._eigvals_ij, self._eigvals_ij))
        eigvecs = np.vstack(
            (self._eigvecs_ii, self._eigvecs_ij, self._eigvecs_ij))
        eigvecs[-len(self._ij):] = eigvecs[-len(self._ij):].conj()
        N = len(self._comm_points)
        qpoints = self._comm_points[self._ii + self._ij + self._ij] / float(N)
        qpoints[-len(self._ij):] = -qpoints[-len(self._ij):]
        d2f = DynmatToForceConstants(
            self._dynmat.primitive,
            self._dynmat.supercell,
            eigenvalues=eigvals,
            eigenvectors=eigvecs,
            commensurate_points=qpoints)
        d2f.run()
        self._force_constants = d2f.force_constants

    def _prepare(self):
        pos = self._dynmat.supercell.scaled_positions
        N = len(self._comm_points)
        for q in self._comm_points[self._ii] / float(N):
            self._dynmat.set_dynamical_matrix(q)
            dm = self._dynmat.dynamical_matrix
            eigvals, eigvecs = np.linalg.eigh(dm.real)
            self._eigvals_ii.append(eigvals)
            self._eigvecs_ii.append(eigvecs)
            self._phase_ii.append(
                np.cos(2 * np.pi * np.dot(pos, q)).reshape(-1, 1))

        for q in self._comm_points[self._ij] / float(N):
            self._dynmat.set_dynamical_matrix(q)
            dm = self._dynmat.dynamical_matrix
            eigvals, eigvecs = np.linalg.eigh(dm)
            self._eigvals_ij.append(eigvals)
            self._eigvecs_ij.append(eigvecs)
            self._phase_ij.append(
                np.exp(2j * np.pi * np.dot(pos, q)).reshape(-1, 1))

    def _solve_ii(self, T, number_of_snapshots, randn=None):
        """

        randn parameter is used for the test.

        """
        natom = self._dynmat.supercell.get_number_of_atoms()
        u = np.zeros((number_of_snapshots, natom, 3), dtype='double')

        shape = (len(self._eigvals_ii), number_of_snapshots,
                 len(self._eigvals_ii[0]))
        if randn is None:
            _randn = np.random.normal(size=shape)
        else:
            _randn = randn
        sigmas = self._get_sigma(self._eigvals_ii, T)
        for dist_func, sigma, eigvecs, phase in zip(
                _randn, sigmas, self._eigvecs_ii, self._phase_ii):
            u_red = np.dot(dist_func * sigma, eigvecs.T).reshape(
                number_of_snapshots, -1, 3)[:, self._s2pp, :]
            u += u_red * phase

        return u

    def _solve_ij(self, T, number_of_snapshots, randn=None):
        """

        randn parameter is used for the test.

        """
        natom = self._dynmat.supercell.get_number_of_atoms()
        u = np.zeros((number_of_snapshots, natom, 3), dtype='double')
        shape = (len(self._eigvals_ij), 2, number_of_snapshots,
                 len(self._eigvals_ij[0]))
        if randn is None:
            _randn = np.random.normal(size=shape)
        else:
            _randn = randn
        sigmas = self._get_sigma(self._eigvals_ij, T)
        for dist_func, sigma, eigvecs, phase in zip(
                _randn, sigmas, self._eigvecs_ij, self._phase_ij):
            u_red = np.dot(dist_func * sigma, eigvecs.T).reshape(
                2, number_of_snapshots, -1, 3)[:, :, self._s2pp, :]
            u += (u_red[0] * phase).real
            u -= (u_red[1] * phase).imag

        return u * np.sqrt(2)

    def _get_sigma(self, eigvals, T):  # max 2D
        freqs = np.sqrt(np.abs(eigvals)) * self._factor
        conditions = freqs > self._cutoff_frequency
        freqs = np.where(conditions, freqs, 1)
        n = np.where(conditions,
                     1.0 / (np.exp(freqs * THzToEv / (Kb * T)) - 1),
                     0)
        sigma = np.where(conditions,
                         np.sqrt(self._unit_conversion / freqs * (0.5 + n)),
                         0)
        return sigma

    def _categorize_points(self):
        N = len(self._comm_points)
        ii = []
        ij = []
        for i, p in enumerate(self._comm_points):
            for j, _p in enumerate(self._comm_points):
                if ((p + _p) % N == 0).all():
                    if i == j:
                        ii.append(i)
                    elif i < j:
                        ij.append(i)
                    break
        return ii, ij
