import numpy as np
from scipy import linalg
from pyscf import gto
from pyscf import lib
from pyscf import scf
from pyscf import df
from pyscf import mcscf
from pyscf.ao2mo import _ao2mo
from pyscf.grad.rhf import GradientsBasics
from pyscf.df.grad.rhf import _int3c_wrapper
from pyscf.ao2mo.outcore import balance_partition
from pyscf.ao2mo.incore import _conc_mos

def get_int3c_mo (mol, auxmol, mo_coeff, compact=False, max_memory=None):
    ''' Evaluate (P|uv) c_ui c_vj -> (P|ij)

    Args:
        mol: gto.Mole
        auxmol: gto.Mole, contains auxbasis
        mo_coeff: ndarray, list, or tuple containing MO coefficients
            if two ndarrays mo_coeff = (mo0, mo1) are provided, mo0 and mo1 are
            used for the two AO dimensions

    Kwargs:
        compact: bool
            If true, will return only unique ERIs along the two MO dimensions.
            Does nothing if mo_coeff contains two different sets of orbitals.
        max_memory: int
            Maximum memory consumption in MB

    Returns:
        int3c: ndarray of shape (naux, nmo0, nmo1) or (naux, nmo*(nmo+1)//2) '''

    nao, naux, nbas, nauxbas = mol.nao, auxmol.nao, mol.nbas, auxmol.nbas
    npair = nao * (nao + 1) // 2
    if max_memory is None: max_memory = mol.max_memory

    # Separate mo_coeff
    if isinstance (mo_coeff, np.ndarray) and mo_coeff.ndim == 2:
        mo0 = mo1 = mo_coeff
    else:
        mo0, mo1 = mo_coeff[0], mo_coeff[1]
    nmo0, nmo1 = mo0.shape[-1], mo1.shape[-1]
    mosym, nmo_pair, mo_conc, mo_slice = _conc_mos(mo0, mo1, compact=compact)

    # (P|uv) -> (P|ij)
    get_int3c = _int3c_wrapper(mol, auxmol, 'int3c2e', 's2ij')
    int3c = np.zeros ((naux, nmo_pair), dtype=mo0.dtype)
    max_memory -= lib.current_memory()[0]    
    blksize = int (min (max (max_memory * 1e6 / 8 / ((npair**2)*2), 20), 240))
    aux_loc = auxmol.ao_loc
    ao_ranges = balance_partition(aux_loc, blksize)
    for shl0, shl1, nL in ao_ranges:
        int3c_ao = get_int3c ((0, nbas, 0, nbas, shl0, shl1))  # (uv|P)
        p0, p1 = aux_loc[shl0], aux_loc[shl1]
        buf = np.zeros ((p1-p0, npair), dtype=int3c_ao.dtype)
        int3c_ao = int3c_ao.T # is apparently stored f-contiguous but in the actual memory order I need, so just transpose
        #assert (int3c_ao.shape == (npair, p1-p0)), 'npair = {}, p1-p0 = {}, shape = {}'.format (npair, p1-p0, int3c_ao.shape)
        assert (int3c_ao.flags.c_contiguous)
        #if ((int3c_ao.shape == (npair, p1-p0) and int3c_ao.flags.c_contiguous) or
        #    (int3c_ao.shape == (p1-p0, npair) and int3c_ao.flags.f_contiguous)):
        #    # I'm pretty sure I have to transpose for _ao2mo.nr_e2 to work
        #    if int3c_ao.flags.f_contiguous: int3c_ao = int3c_ao.T
        #    assert (int3c_ao.flags.c_contiguous)
        #    int3c_ao = lib.transpose (int3c_ao, out=buf)
        int3c[p0:p1] = _ao2mo.nr_e2(int3c_ao, mo_conc, mo_slice, aosym='s2', mosym=mosym, out=int3c[p0:p1])
        int3c_ao = None

    # Shape and return
    if 's1' in mosym: int3c = int3c.reshape (naux, nmo0, nmo1)
    return int3c

def solve_df_rdm2 (mc_or_mc_grad, mo_cas=None, ci=None, casdm2=None):
    ''' Solve (P|Q)d_Qij = (P|kl)d_ijkl for d_Qij in the MO basis.

    Args:
        mc_or_mc_grad: DF-MCSCF energy or gradients method object.

    Kwargs:
        mo_cas: ndarray, tuple, or list containing active mo coefficients.
            if two ndarrays mo_cas = (mo0, mo1) are provided, mo0 and mo1 are
            assumed to correspond to casdm2's LAST two dimensions in that order,
            regardless of len (ci) or len (casdm2).
            (This will facilitate SA-CASSCF gradients at some point. Note the
            difference from grad_elec_dferi!)
        ci: ndarray, tuple, or list containing CI coefficients in mo_cas basis.
            Not used if casdm2 is provided.
        casdm2: ndarray, tuple, or list containing rdm2 in mo_cas basis.
            Computed by mc_or_mc_grad.fcisolver.make_rdm12 (ci,...) if omitted.
        
    Returns:
        dfcasdm2: ndarray or list containing 3-center 2RDM, d_Pqr, where P is
            auxbasis index and q, r are mo_cas basis indices. '''

    # Initialize mol and auxmol
    mol = mc_or_mc_grad.mol
    if isinstance (mc_or_mc_grad, GradientsBasics):
        mc = mc_or_mc_grad.base
    else:
        mc = mc_or_mc_grad
    auxmol = mc.with_df.auxmol
    if auxmol is None:
        auxmol = df.addons.make_auxmol(mc.with_df.mol, mc.with_df.auxbasis)
    nao, naux, nbas, nauxbas = mol.nao, auxmol.nao, mol.nbas, auxmol.nbas
    npair = nao * (nao + 1) // 2
    ncore, ncas, nelecas = mc.ncore, mc.ncas, mc.nelecas
    nocc = ncore + ncas

    # Initialize casdm2, mo_cas, and nset
    if mo_cas is None: mo_cas = mc_or_mc_grad.mo_coeff[:,ncore:nocc]
    if ci is None: ci = mc_or_mc_grad.ci
    if casdm2 is None: casdm2 = mc.fcisolver.make_rdm12 (ci, ncas, nelecas)
    if np.asarray (casdm2).ndim == 4: casdm2 = [casdm2]
    nset = len (casdm2)

    # (P|Q) and (P|ij)
    int2c = linalg.cho_factor(auxmol.intor('int2c2e', aosym='s1'))
    int3c = get_int3c_mo (mol, auxmol, mo_cas, compact=True, max_memory=mc_or_mc_grad.max_memory)

    # Solve (P|Q) d_Qij = (P|kl) d_ijkl
    dfcasdm2 = []
    for dm2 in casdm2:
        nmo = tuple (dm2.shape) # make sure it copies
        if int3c.ndim == 2:
            # I'm not going to use the memory-efficient version because this is meant to be small
            nmo_pair = nmo[2] * (nmo[2] + 1) // 2
            dm2 = dm2.reshape ((-1, nmo[2], nmo[3]))
            dm2 += dm2.transpose (0,2,1)
            diag_idx = np.arange(nmo[-1])
            diag_idx = diag_idx * (diag_idx+1) // 2 + diag_idx
            dm2 = lib.pack_tril (np.ascontiguousarray (dm2))
            dm2[:,diag_idx] *= 0.5
        elif int3c.ndim == 3:
            nmo_pair = nmo[2] * nmo[3]
            int3c = int3c.reshape (naux, nmo_pair)
        else:
            raise RuntimeError ('int3c.shape = {}'.format (int3c.shape))
        dm2 = dm2.reshape (nmo[0]*nmo[1], nmo_pair).T
        int3c_dm2 = np.dot (int3c, dm2)
        dfcasdm2.append (linalg.cho_solve (int2c, int3c_dm2).reshape (naux, nmo[0], nmo[1]))

    return dfcasdm2

def energy_elec_dferi (mc, mo_cas=None, ci=None, dfcasdm2=None, casdm2=None):
    ''' Evaluate E2 = (P|qr)d_Pqr/2, where d_Pqr is the DF-2rdm obtained by solve_df_rdm2.
    For testing purposes. 

    Args:
        mc: MC-SCF energy method object

    Kwargs:
        mo_cas: ndarray, list, or tuple containing active-space MO coefficients
            If a tuple of length 2, the same pair of MO sets are assumed to apply to
            the internally-contracted and externally-contracted indices of the DF-2rdm:
            (P|Q)d_Qij = (P|kl)d_ijkl -> (P|Q)d_Qij = (P|ij)d_ijij
            If a tuple of length 4, the 4 MO sets are applied to ijkl above in that order
            (first two external, last two internal)
        ci: ndarray, tuple, or list containing CI coefficients in mo_cas basis.
            Not used if dfcasdm2 is provided.
        dfcasdm2: ndarray, tuple, or list containing DF-2rdm in mo_cas basis.
            Computed by solve_df_rdm2 if omitted.
        casdm2: ndarray, tuple, or list containing rdm2 in mo_cas basis.
            Computed by mc_or_mc_grad.fcisolver.make_rdm12 (ci,...) if omitted.
    '''
    if isinstance (mc, GradientsBasics): mc = mc.base
    if mo_cas is None:
        ncore = mc.ncore
        nocc = ncore + mc.ncas
        mo_cas = mc.mo_coeff[:,ncore:nocc]
    fac = 0.5
    if isinstance (mo_cas, np.ndarray) and mo_cas.ndim == 2:
        mo_cas = (mo_cas,)*4
    elif len (mo_cas) == 2:
        mo_cas = (mo_cas[0], mo_cas[1], mo_cas[0], mo_cas[1])
    elif len (mo_cas) == 4:
        mo_cas = tuple (mo_cas)
        fac = 1.0
    else:
        raise RuntimeError ('Invalid shape of np.asarray (mo_cas): {}'.format (mo_cas.shape))
    nmo = [mo.shape[1] for mo in mo_cas]
    if ci is None: ci = mc.ci
    if dfcasdm2 is None: dfcasdm2 = solve_df_rdm2 (mc, mo_cas=mo_cas[2:], ci=ci, casdm2=casdm2)
    int3c = get_int3c_mo (mc.mol, mc.with_df.auxmol, mo_cas[:2], compact=True, max_memory=mc.max_memory)
    symm = (int3c.ndim == 2)
    int3c = np.ravel (int3c)
    energy = []
    for dm2 in dfcasdm2:
        naux = mc.with_df.auxmol.nao
        if symm:
            nmo_pair = nmo[0] * (nmo[0] + 1) // 2
            dm2 += dm2.transpose (0,2,1)
            diag_idx = np.arange(nmo[1])
            diag_idx = diag_idx * (diag_idx+1) // 2 + diag_idx
            dm2 = lib.pack_tril (np.ascontiguousarray (dm2))
            dm2[:,diag_idx] *= 0.5
        else:
            nmo_pair = nmo[0] * nmo[1]
        energy.append (np.dot (int3c, dm2.ravel ()) * fac)

    return energy

def grad_elec_dferi (mc_grad, mo_cas=None, ci=None, dfcasdm2=None, atmlst=None):
    ''' Evaluate the electronic gradient using the DF-2rdm obtained by solve_df_rdm2.

    Args:
        mc_grad: MC-SCF gradients method object

    Kwargs:
        mc_cas: ndarray, list, or tuple containing active-space MO coefficients
            if two ndarrays mo_cas = (mo0, mo1) are provided, mo0 and mo1 are
            assumed to correspond to dfcasdm2's two MO dimensions in that order,
            regardless of len (ci) or len (dfcasdm2).
            (This will facilitate SA-CASSCF gradients at some point. Note
            the difference from solve_df_rdm2!)
        ci: ndarray, tuple, or list containing CI coefficients in mo_cas basis.
            Not used if dfcasdm2 is provided.
        dfcasdm2: ndarray, tuple, or list containing DF-2rdm in mo_cas basis.
            Computed by solve_df_rdm2 if omitted.
        atmlst: list of integers
            List of nonfrozen atoms, as in grad_elec functions.
            Defaults to list (range (mol.natm))

    Returns:
        gradient: ndarray of shape (len (atmlst), 3) '''

    mol = mc_grad.mol
    auxmol = mc_grad.base.with_df.auxmol
    ncore, ncas, nao, naux = mc_grad.ncore, mc_grad.ncas, mol.nao, auxmol.nao
    nocc = ncore + ncas
    if mo_cas is None: mo_cas = mc_grad.mo_coeff[:,ncore:nocc]
    if ci is None: ci = mc.ci
    if dfcasdm2 is None: dfcasdm2 = solve_df_rdm2 (mc, mo_cas=mo_cas, ci=ci)



if __name__ == '__main__':
    from pyscf.tools import molden
    from pyscf.lib import logger
    h2co_casscf66_631g_xyz = '''C  0.534004  0.000000  0.000000
    O -0.676110  0.000000  0.000000
    H  1.102430  0.000000  0.920125
    H  1.102430  0.000000 -0.920125'''
    mol = gto.M (atom = h2co_casscf66_631g_xyz, basis = '6-31g', symmetry = False, verbose = logger.INFO, output = 'h2co_casscf66_631g_grad.log')
    mf = scf.RHF (mol).density_fit (auxbasis = df.aug_etb (mol)).run ()
    mc = mcscf.CASSCF (mf, 6, 6)
    mc.kernel ()

    ncore, ncas = mc.ncore, mc.ncas
    nocc = ncore + ncas
    mo_cas = mc.mo_coeff[:,ncore:nocc]
    casdm2 = mc.fcisolver.make_rdm12 (mc.ci, mc.ncas, mc.nelecas)[1]
    eri_cas = mc.with_df.ao2mo (mo_cas, compact=False).reshape ((ncas,)*4)

    # Full energy
    e2_ref = np.dot (casdm2.ravel (), eri_cas.ravel ()) / 2
    e2_test = energy_elec_dferi (mc, mo_cas=mo_cas, casdm2=casdm2)[0]
    e2_err = e2_test - e2_ref
    print ("Testing full energy calculation: e2_test - e2_ref = {:13.6e} - {:13.6e} = {:13.6e}".format (e2_test, e2_ref, e2_err))

    # 2-RDM slices (see: SA-CASSCF gradients)
    eri_cas_sl = eri_cas[0:2,1:3,0:4,1:6]
    casdm2_sl = casdm2[0:2,1:3,0:4,1:6]
    mo_cas_sl = (mo_cas[:,0:2], mo_cas[:,1:3], mo_cas[:,0:4], mo_cas[:,1:6]) 
    e2_ref = np.dot (casdm2_sl.ravel (), eri_cas_sl.ravel ())
    e2_test = energy_elec_dferi (mc, mo_cas=mo_cas_sl, casdm2=casdm2_sl)[0]
    e2_err = e2_test - e2_ref
    print ("Testing slice calculation: e2_test - e2_ref = {:13.6e} - {:13.6e} = {:13.6e}".format (e2_test, e2_ref, e2_err))

    # 2-RDM slice complex conjugate
    casdm2_sl = casdm2[1:3,0:2,1:6,0:4]
    eri_cas_sl = eri_cas[1:3,0:2,1:6,0:4]
    mo_cas_sl = (mo_cas[:,1:3], mo_cas[:,0:2], mo_cas[:,1:6], mo_cas[:,0:4]) 
    e2_test = np.dot (casdm2_sl.ravel (), eri_cas_sl.ravel ())
    e2_err = e2_test - e2_ref
    print ("Testing slice c.c. ERI: e2_test - e2_ref = {:13.6e} - {:13.6e} = {:13.6e}".format (e2_test, e2_ref, e2_err))
    e2_test = energy_elec_dferi (mc, mo_cas=mo_cas_sl, casdm2=casdm2_sl)[0]
    print ("Testing slice c.c. DFERI: e2_test - e2_ref = {:13.6e} - {:13.6e} = {:13.6e}".format (e2_test, e2_ref, e2_err))
    
    # 2-RDM slice electron interchange
    casdm2_sl = casdm2[0:4,1:6,0:2,1:3]
    eri_cas_sl = eri_cas[0:4,1:6,0:2,1:3]
    mo_cas_sl = (mo_cas[:,0:4], mo_cas[:,1:6], mo_cas[:,0:2], mo_cas[:,1:3]) 
    e2_test = np.dot (casdm2_sl.ravel (), eri_cas_sl.ravel ())
    e2_err = e2_test - e2_ref
    print ("Testing slice 1<->2 ERI: e2_test - e2_ref = {:13.6e} - {:13.6e} = {:13.6e}".format (e2_test, e2_ref, e2_err))
    e2_test = energy_elec_dferi (mc, mo_cas=mo_cas_sl, casdm2=casdm2_sl)[0]
    print ("Testing slice 1<->2 DFERI: e2_test - e2_ref = {:13.6e} - {:13.6e} = {:13.6e}".format (e2_test, e2_ref, e2_err))


