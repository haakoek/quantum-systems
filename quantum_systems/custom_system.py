import warnings

from quantum_systems.system import QuantumSystem
from quantum_systems.system_helper import (
    transform_one_body_elements,
    transform_two_body_elements,
    add_spin_one_body,
    add_spin_two_body,
    anti_symmetrize_u,
)


class CustomSystem(QuantumSystem):
    """Custom quantum system where a user can pass in matrix elements from
    other sources. The purpose of this class is to allow usage of quantum
    solvers made by the author and collaborators using other sources of matrix
    elements.
    """

    def set_h(self, h, add_spin=False):
        if add_spin:
            h = add_spin_one_body(h, np=self.np)

        assert all(
            self.l == axis for axis in h.shape
        ), "Shape of one-body tensor must match the number of orbitals"

        self._h = h

    def set_u(self, u, add_spin=False, anti_symmetrize=False):
        if add_spin:
            u = add_spin_two_body(u, np=self.np)

        if anti_symmetrize:
            u = anti_symmetrize_u(u)

        assert all(
            self.l == axis for axis in u.shape
        ), "Shape of two-body tensor axis must match the number of orbitals"

        self._u = u

    def set_s(self, s, add_spin=False):
        if add_spin:
            s = add_spin_one_body(s, np=self.np)

        assert all(
            self.l == axis for axis in s.shape
        ), "Shape of overlap tensor must match the number of orbitals"

        self._s = s

    def set_dipole_moment(self, dipole_moment, add_spin=False):
        np = self.np

        if len(dipole_moment.shape) < 3:
            dipole_moment = np.array([dipole_moment])

        if not add_spin:
            self._dipole_moment = dipole_moment
            return

        new_shape = [dipole_moment.shape[0]]
        new_shape.extend(list(map(lambda x: x * 2, dipole_moment.shape[1:])))

        assert all(
            self.l == axis for axis in new_shape[1:]
        ), "Shape of dipole moment matrices must match the number of orbitals"

        self._dipole_moment = np.zeros(tuple(new_shape))

        for i in range(len(dipole_moment)):
            self._dipole_moment[i] = add_spin_one_body(dipole_moment[i], np=np)

    def set_nuclear_repulsion_energy(self, nuclear_repulsion_energy):
        self._nuclear_repulsion_energy = nuclear_repulsion_energy


def construct_pyscf_system_ao(molecule, basis="cc-pvdz", np=None):
    import pyscf

    if np is None:
        import numpy as np

    mol = pyscf.gto.Mole()
    mol.unit = "bohr"
    mol.build(atom=molecule, basis=basis, symmetry=False)
    mol.set_common_origin(np.array([0.0, 0.0, 0.0]))

    n = mol.nelectron
    l = mol.nao * 2

    h = pyscf.scf.hf.get_hcore(mol)
    s = mol.intor_symmetric("int1e_ovlp")
    u = (
        mol.intor("int2e")
        .reshape(l // 2, l // 2, l // 2, l // 2)
        .transpose(0, 2, 1, 3)
    )
    dipole_integrals = mol.intor("int1e_r").reshape(3, l // 2, l // 2)

    system = CustomSystem(n, l, np=np)
    system.set_h(h, add_spin=True)
    system.set_s(s, add_spin=True)
    system.set_u(u, add_spin=True, anti_symmetrize=True)
    system.set_dipole_moment(dipole_integrals, add_spin=True)
    system.cast_to_complex()

    return system


def construct_pyscf_system(molecule, basis="cc-pvdz", np=None, verbose=False):
    import pyscf

    if np is None:
        import numpy as np

    # Build molecule in AO-basis
    mol = pyscf.gto.Mole()
    mol.unit = "bohr"
    mol.build(atom=molecule, basis=basis, symmetry=False)
    mol.set_common_origin(np.array([0.0, 0.0, 0.0]))

    # Perform UHF-calculations to create the MO-basis
    hf = pyscf.scf.UHF(mol)
    ehf = hf.kernel()

    if not hf.converged:
        warnings.warn("UHF calculation did not converge")

    if verbose:
        print(f"UHF energy: {hf.e_tot}")

    # Build the coefficient matrix from the occupied and virtual integrals. As
    # we have done a UHF-calculation, we stack the two spin-directions on top
    # of each other. That is, instead of using odd or even indices for each spin
    # direction, we set up two separate blocks.
    C_o = np.hstack(
        (
            # Fetch occupied coefficients for both spin-directions
            hf.mo_coeff[0][:, hf.mo_occ[0] > 0],
            hf.mo_coeff[1][:, hf.mo_occ[1] > 0],
        )
    )

    C_v = np.hstack(
        (
            # Fetch virtual coefficients for both spin-directions
            hf.mo_coeff[0][:, hf.mo_occ[0] == 0],
            hf.mo_coeff[1][:, hf.mo_occ[1] == 0],
        )
    )

    # Build full coefficient matrix.
    C = np.hstack((C_o, C_v))

    # Get the number of occupied molecular orbitals
    n = C_o.shape[1]
    # Fetch the number of molecular orbitals
    l = C.shape[1]

    # Check that the number of occupied molecular orbitals is correct
    assert n == sum(hf.mo_occ[0] > 0) + sum(hf.mo_occ[1] > 0)
    # Check that the number of molecular orbitals is twice of that of the number
    # of atomic orbitals.
    assert l == C.shape[0] * 2

    # Note: Should the dipole moments have a negative sign?
    dipole_moment = [
        -transform_one_body_elements(dm, C, np)
        for dm in mol.intor("int1e_r").reshape(3, mol.nao, mol.nao)
    ]
    dipole_moment = np.asarray(dipole_moment)

    # Create a tuple with the shape of the AO two-body elements
    u_shape = (mol.nao for i in range(4))

    h = transform_one_body_elements(hf.get_hcore(), C, np)
    u = transform_two_body_elements(mol.intor("int2e").reshape(*u_shape), C, np)

    noa = sum(hf.mo_occ[0] > 0)
    nva = sum(hf.mo_occ[0] == 0)
    nob = sum(hf.mo_occ[1] > 0)
    nvb = sum(hf.mo_occ[1] == 0)
    no = noa + nob
    nv = nva + nvb

    oa = slice(0, noa)
    ob = slice(noa, no)
    va = slice(no, no + nva)
    vb = slice(no + nva, no + nv)

    a_slices = [oa, va]
    b_slices = [ob, vb]

    # Create a combination of slices that should be zero in all matrix elements,
    # due to unequal spin-direction.
    zero_slices = [(a, b) for a in a_slices for b in b_slices]
    zero_slices += [(b, a) for a in a_slices for b in b_slices]

    # Create a slice object for all indices, i.e., the ":" syntax in NumPy.
    all_slice = slice(None, None)

    # Explicitly set all cross-spin terms to zero
    for s in zero_slices:
        h[s] = 0
        dipole_moment[(all_slice,) + s] = 0
        u[s + (all_slice, all_slice)] = 0
        u[(all_slice, all_slice) + s] = 0

    # Convert to physicist's notation, from Mulliken notation
    u = u.transpose(0, 2, 1, 3)

    # Build a custom system from the integral elements
    system = CustomSystem(n, l, np=np)
    system.set_h(h)
    system.set_u(u, anti_symmetrize=True)
    system.set_dipole_moment(dipole_moment)
    system.cast_to_complex()

    return system


def construct_psi4_system(molecule, options, np=None):
    import psi4

    if np is None:
        import numpy as np

    psi4.core.be_quiet()
    psi4.set_options(options)

    mol = psi4.geometry(molecule)
    nuclear_repulsion_energy = mol.nuclear_repulsion_energy()

    wavefunction = psi4.core.Wavefunction.build(
        mol, psi4.core.get_global_option("BASIS")
    )

    molecular_integrals = psi4.core.MintsHelper(wavefunction.basisset())

    kinetic = np.asarray(molecular_integrals.ao_kinetic())
    potential = np.asarray(molecular_integrals.ao_potential())
    h = kinetic + potential

    u = np.asarray(molecular_integrals.ao_eri()).transpose(0, 2, 1, 3)
    overlap = np.asarray(molecular_integrals.ao_overlap())

    n = wavefunction.nalpha() + wavefunction.nbeta()
    l = 2 * wavefunction.nmo()

    dipole_integrals = [
        np.asarray(mu) for mu in molecular_integrals.ao_dipole()
    ]
    dipole_integrals = np.stack(dipole_integrals)

    system = CustomSystem(n, l, np=np)
    system.set_h(h, add_spin=True)
    system.set_u(u, add_spin=True, anti_symmetrize=True)
    system.set_s(overlap, add_spin=True)
    system.set_dipole_moment(dipole_integrals, add_spin=True)
    system.set_nuclear_repulsion_energy(nuclear_repulsion_energy)

    return system
