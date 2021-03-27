import numpy as np
from scipy import linalg
from pyscf import gto, scf, lib, mcscf
from mrh.my_pyscf.mcscf.lasscf_testing import LASSCF
from mrh.exploratory.citools import fockspace
from mrh.exploratory.unitary_cc import lasuccsd 

xyz = '''H 0.0 0.0 0.0
         H 1.0 0.0 0.0
         H 0.2 3.9 0.1
         H 1.159166 4.1 -0.1'''
mol = gto.M (atom = xyz, basis = 'sto-3g', output='h4_sto3g.log',
    verbose=lib.logger.DEBUG)
mf = scf.RHF (mol).run ()
ref = mcscf.CASSCF (mf, 4, 4).run () # = FCI
las = LASSCF (mf, (2,2), (2,2), spin_sub=(1,1))
frag_atom_list = ((0,1),(2,3))
mo_loc = las.localize_init_guess (frag_atom_list, mf.mo_coeff)
las.kernel (mo_loc)

# LASUCC is implemented as a FCI solver for MC-SCF
# It's compatible with CASSCF as well as CASCI, but it's really slow
mc = mcscf.CASCI (mf, 4, 4)
mc.mo_coeff = las.mo_coeff
mc.fcisolver = lasuccsd.FCISolver (mol)
mc.fcisolver.nlas = [2,2] # Number of orbitals per fragment
mc.kernel ()

print ("FCI energy:      {:.9f}".format (ref.e_tot))
print ("LASSCF energy:   {:.9f}".format (las.e_tot))
print ("LASUCCSD energy: {:.9f}\n".format (mc.e_tot))

# It's a bit opaque but this is an object I use to set up the BFGS
# and store intermediates. I cache it at the end.
# All of the CI vecs below are in Fock space <s>because I'm an idiot</s>
obj_fn = mc.fcisolver._obj_fn

res = obj_fn.res # OptimizeResult object returned by scipy.optimize.minimize
                 # see docs.scipy.org for more documentation about this
x = res.x # Amplitude vector for the BFGS problem 
energy, gradient = obj_fn (x) # obj_fn is callable!
print ("Recomputing LASUCC total energy with cached objective function")
print ("LASUCCSD energy: {:.9f}".format (energy))
print ("|gradient| = {:.3e}".format (linalg.norm (gradient)))
print ("If that seems too high to you, consider: BFGS sucks.\n")

fcivec = obj_fn.get_fcivec (x) # |LASUCC> itself as a CI vector
ss, multip = mc.fcisolver.spin_square (fcivec, 4, 'ThisArgDoesntMatter')
print ("<LASUCC|S^2|LASUCC> = {:.3f}; apparent S = {:.1f}".format (
    ss, 0.5*(multip-1)))
print ("But is that really the case?")
print ("Singlet weight: {:.2f}".format (fockspace.hilbert_sector_weight(
    fcivec, 4, (2,2), 1)))
print ("Triplet weight: {:.2f}".format (fockspace.hilbert_sector_weight(
    fcivec, 4, (2,2), 3)))
print ("Quintet weight: {:.2f}".format (fockspace.hilbert_sector_weight(
    fcivec, 4, (2,2), 5)))
print ("Oh well, I guess it couldn't have been anything else.\n")

ci_f = obj_fn.get_ci_f (x) # list of optimized CI vectors for each fragment
ci_h = [fockspace.fock2hilbert (c, 2, (1,1)) for c in ci_f]
w_nb = [linalg.norm (c_f)**2 - linalg.norm (c_h)**2 for (c_f, c_h) in 
    zip (ci_f, ci_h)]
print (("Wave function weight outside of the singlet 2-electron "
        "Hilbert space"))
print (("(If these numbers are nonzero, then my implementation "
        "of UCC doesn't pointlessly waste memory)"))
for ix in range (2):
    print ("Fragment {}: {:.1e}".format (ix, w_nb[ix]))

# U'HU for a single fragment can be retrieved as a
# LASUCCEffectiveHamiltonian object, which is just the ndarray (in 
# the member "full") and some convenience functions
heff = obj_fn.get_dense_heff (x, 0)
print ("\nThe shape of the dense matrix U'HU for the first fragment is",
    heff.full.shape)
hc_f = np.dot (heff.full, ci_f[0].ravel ())
chc_f = np.dot (ci_f[0].ravel ().conj (), hc_f)
print (("Recomputing LASUCCSD total energy from the effective "
        "Hamiltonian of the 1st fragment and its optimized CI vector"))
print ("LASUCCSD energy: {:.9f}".format (chc_f))
diag_err = linalg.norm (hc_f - (ci_f[0].ravel ()*chc_f))
print (("CI vector diagonalization error: {:.3e}\n".format (diag_err)))

# There are a couple of different ways to expose the tiny useful part
# of the LASUCCEffectiveHamiltonian array
heff_non0, idx_non0 = heff.get_nonzero_elements ()
neleca, nelecb = 1,1
nelec_bra = (neleca, nelecb)
nelec_ket = (neleca, nelecb)
heff_11, idx_11 = heff.get_number_block (nelec_bra, nelec_ket)

# In spinless Fock space, determinants can be so ordered that the occupation
# number vector is equal to the binary representation of its ordinal index.
# Taking advantage of this, I wrote a function in fockspace that takes two 
# integers, one for spin-up and the other for spin-down electrons, and returns
# an ONV string for the spin-symmetric basis (i.e., elements have value 0, a, b, 
# or 2). Since PySCF has the normal-order convention that any spin-up operator 
# is to the left of all spin-down operators, you can work out that the relation
# between the spinless determinant and the two spin-separated determinants is
# simply det_a, det_b = divmod (det_spinless, 2**norb), where norb is the 
# number of spin-down spinorbitals.
print (("The effective Hamiltonian of the first fragment has {} nonzero"
        " elements.").format (len (heff_non0)))
print ("They are, in no particular order:")
idx_bra, idx_ket = np.where (idx_non0)
print ("{:>8s}  {:>3s}  {:>3s}  {:>13s}".format ("Index",
    "Bra", "Ket", "Value"))
for bra_spinless, ket_spinless, el in zip (idx_bra, idx_ket, heff_non0):
    idx = "({},{})".format (bra_spinless, ket_spinless)
    bra_a, bra_b = divmod (bra_spinless, 4) 
    bra_onv = fockspace.onv_str (bra_a, bra_b, 2)
    ket_a, ket_b = divmod (ket_spinless, 4)
    ket_onv = fockspace.onv_str (ket_a, ket_b, 2)
    print ("{:>8s}  {:>3s}  {:>3s}  {:13.6e}".format (idx, bra_onv,
           ket_onv, el))
print (("The diagonal 2-electron sz=0 block of the first effective "
        "Hamiltonian of the first fragment is:"))
print (heff_11)
idx_spinless = np.squeeze (idx_11[0])
print ("The basis is:")
for ix, det_spinless in enumerate (idx_spinless):
    deta, detb = divmod (det_spinless, 4)
    det_onv = fockspace.onv_str (deta, detb, 2)
    print ("{} {}".format (ix, det_onv))
print ("The eigenspectrum of this block is:")
print (linalg.eigh (heff_11)[0])

