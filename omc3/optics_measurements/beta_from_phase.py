"""
.. module: beta_from_phase

Created on 27/05/13

:author: Viktor Maier, Andreas Wegscheider, Lukas Malina

It computes betas and alphas from phase advances.
"""
import os
import re
import numpy as np
import pandas as pd
from scipy.linalg import circulant
import tfs
from utils import logging_tools, stats
from optics_measurements.toolbox import df_rel_diff, df_ratio, df_diff
from optics_measurements.constants import BETA_NAME, EXT, ERR, DELTA, MDL

__version__ = "2019.0.a"
LOGGER = logging_tools.get_logger(__name__)

TWOPI = 2 * np.pi
EPSILON = 1.0E-16
ZERO_THRESHOLD = 1e-3
COT_THRESHOLD = 15.9
RCOND = 1.0e-10

METH_3BPM = "3BPM method"
METH_A_NBPM = "Analytical N-BPM method"
METH_NO_ERR = "No Errors"


def calculate(meas_input, tune_dict, phase_dict, header_dict, plane):
    """
    Calculates betas and alphas from phase advances
    Args:
        meas_input: OpticsInput object
        tune_dict: TuneDict contains measured tunes
        phase_dict: PhaseDict contains measured phase advances
        header_dict:  dictionary of header items common for all output files
        plane: plane

    Returns:
        BetaDict object containing specific TfsDataFrames with results
    """
    if meas_input.compensation == "none" and meas_input.accelerator.excitation:
        meas_and_model_tunes = (tune_dict[plane]["Q"], tune_dict[plane]["QM"] % 1)
        model = meas_input.accelerator.get_driven_tfs()
        bk_model = model  # TODO we need driven bk model
    else:
        meas_and_model_tunes = (tune_dict[plane]["QF"], tune_dict[plane]["QFM"] % 1)
        model = meas_input.accelerator.get_model_tfs()
        try:
            bk_model = meas_input.accelerator.get_best_knowledge_model_tfs()
        except AttributeError:
            LOGGER.debug("No best knowledge model - using the normal one.")
            bk_model = model

    elements = meas_input.accelerator.get_elements_tfs().loc[:, ["S", "K1L", "K2L", f"MU{plane}", f"BET{plane}"]]
    if meas_input.three_bpm_method:
        error_method = METH_3BPM
    else:
        LOGGER.debug("Accelerator Error Definition")
        error_defs_path = meas_input.accelerator.get_errordefspath()
        if error_defs_path is None:
            raise IOError(f"Error definition file could not be found")
        elements = _assign_uncertainties(elements, error_defs_path)
        errors_assigned = (len(elements["dK1"].nonzero()[0]) + len(
            elements["dX"].nonzero()[0]) + len(elements["KdS"].nonzero()[0])) > 0
        if not errors_assigned:
            LOGGER.warning("No systematic errors were given or no element was found for the given "
                           "error definitions. The systematic lattice errors are not used.")
        error_method = METH_A_NBPM if errors_assigned else METH_NO_ERR
    LOGGER.info(f"Errors from {error_method}")
    beta_df, rmsbb = betas_alphas_from_phase(bk_model, model, elements, phase_dict, plane, meas_input.range_of_bpms, error_method, meas_and_model_tunes)
    header = _get_header(header_dict, error_method, meas_input.range_of_bpms, rmsbb)
    return beta_df, header


def write(beta_df, header, outputdir, plane):
    tfs.write(os.path.join(outputdir, f"{BETA_NAME}{plane.lower()}{EXT}"), beta_df,
              header, save_index="NAME")


def betas_alphas_from_phase(bk_model, model, elements, phase, plane, range_of_bpms, errors_method, meas_and_model_tunes):
    """
    Calculates betas and alphas from phase using specified method
    Args:
        bk_model: Best knowledge model tfs
        model: Nominal model tfs
        elements: Model with all necessary elements and errors
        phase: phase matrices of measurement with errors and model tfs (bpm x bpm)
        plane: plane either X or Y
        range_of_bpms: size of a range centered at probed BPM
        errors_method: specified method
        meas_and_model_tunes: measured  and model tunes

    Returns:
        tfs.DataFrame containing betas and alfas from phase
    """
    beta_df = tfs.TfsDataFrame(model).loc[phase["MEAS"].index, ["S", f"BET{plane}", f"ALF{plane}"]]
    beta_df = beta_df.rename(columns={f"BET{plane}": f"BET{plane}{MDL}", f"ALF{plane}": f"ALF{plane}{MDL}"})
    if errors_method == METH_3BPM:
        beta_df = three_bpm_method(phase, plane, meas_and_model_tunes, beta_df)
    else:
        beta_df = n_bpm_method(bk_model.loc[phase["MEAS"].index, :], elements, phase, plane, range_of_bpms, meas_and_model_tunes, beta_df)
    beta_df[f"{DELTA}BET{plane}"] = df_rel_diff(beta_df, f"BET{plane}", f"BET{plane}{MDL}")
    beta_df[f"{ERR}{DELTA}BET{plane}"] = df_ratio(beta_df, f"{ERR}BET{plane}", f"BET{plane}{MDL}")
    beta_df[f"{DELTA}ALF{plane}"] = df_diff(beta_df, f"ALF{plane}", f"ALF{plane}{MDL}")
    beta_df[f"{ERR}{DELTA}ALF{plane}"] = beta_df.loc[:, f"{ERR}ALF{plane}"].values
    rmsbb = stats.weighted_rms(beta_df.loc[:, f"{DELTA}BET{plane}"].values) * 100
    LOGGER.info(f" - RMS beta beat: {rmsbb:.3f}%")
    return beta_df, rmsbb


def n_bpm_method(bk_model, elements, phase, plane, range_of_bpms, meas_and_mdl_tunes, beta_df):
    """
    Calculates betas and alphas from using all BPM combination within range_of_bpms,
    it also accounts for systematic errors
    Args:
        bk_model: Best knowledge model tfs
        elements: Model with all necessary elements and errors
        phase: phase matrices of measurement with errors and model tfs (bpm x bpm)
        plane: plane either X or Y
        range_of_bpms: size of a range centered at probed BPM
        meas_and_mdl_tunes: measured  and model tunes
        beta_df: tfs skeleton

    Returns:
        tfs.DataFrame containing betas and alfas from phase
    """
    tune, mdltune = meas_and_mdl_tunes
    betas_alfas = np.zeros((len(phase["MEAS"].index), 4))
    nbpms = len(bk_model.index)
    n_comb = np.zeros(nbpms, dtype=int)
    m = int(range_of_bpms / 2)
    loc_range = np.arange(-m, m + 1)
    phases_meas = phase["MEAS"] * TWOPI
    phases_err = phase["ERRMEAS"] * TWOPI
    phases_err.where(phases_err.notnull(), 1, inplace=True)

    for indx, probed_bpm_name in enumerate(bk_model.index):
        indx_el_first = elements.index.get_loc(bk_model.index[(indx - m) % nbpms])
        indx_el_last = elements.index.get_loc(bk_model.index[(indx + m) % nbpms])
        mu_column = "MU" + plane
        if indx < m:
            outer_meas_phase_adv = pd.concat((phases_meas.iloc[indx, nbpms + indx - m:] - tune * TWOPI, phases_meas.iloc[indx, :indx + m + 1]))
            outer_meas_err = pd.concat((phases_err.iloc[indx, nbpms + indx - m:], phases_err.iloc[indx, :indx + m + 1]))
            outer_mdl_ph = np.concatenate((bk_model.iloc[nbpms + indx - m:][mu_column] - mdltune, bk_model.iloc[:indx + m + 1][mu_column])) * TWOPI
            outer_elmts = pd.concat((elements.iloc[indx_el_first:], elements.iloc[:indx_el_last + 1]))
            outer_elmts_ph = np.concatenate((elements.iloc[indx_el_first:][mu_column] - mdltune, elements.iloc[:indx_el_last + 1][mu_column])) * TWOPI
        elif indx + m >= nbpms:
            outer_meas_phase_adv = pd.concat((phases_meas.iloc[indx, indx - m:], phases_meas.iloc[indx, :indx + m + 1 - nbpms] + tune * TWOPI))
            outer_meas_err = pd.concat((phases_err.iloc[indx, indx - m:], phases_err.iloc[indx, :indx + m + 1 - nbpms]))
            outer_mdl_ph = np.concatenate((bk_model.iloc[indx - m:][mu_column], bk_model.iloc[:indx + m + 1 - nbpms][mu_column] + mdltune)) * TWOPI
            outer_elmts = pd.concat((elements.iloc[indx_el_first:], elements.iloc[:indx_el_last + 1]))
            outer_elmts_ph = np.concatenate((elements.iloc[indx_el_first:][mu_column], elements.iloc[:indx_el_last + 1][mu_column] + mdltune)) * TWOPI
        else:
            outer_meas_phase_adv = phases_meas.iloc[indx, indx + loc_range]
            outer_meas_err = phases_err.iloc[indx, indx + loc_range]
            outer_mdl_ph = bk_model.iloc[indx + loc_range][mu_column].values * TWOPI
            outer_elmts = elements.iloc[indx_el_first:indx_el_last + 1]
            outer_elmts_ph = elements.iloc[indx_el_first:indx_el_last + 1][mu_column] * TWOPI
        bpms_inds_elements = [outer_elmts.index.get_loc(bpm_name) for bpm_name in outer_meas_phase_adv.index.values]
        sin_squared_elements = np.square(np.sin(outer_elmts_ph[:, np.newaxis] - outer_mdl_ph[np.newaxis, :]))
        with np.errstate(divide='ignore'):
            cot_meas = 1.0 / np.tan(outer_meas_phase_adv.values)
            cot_model = 1.0 / np.tan((outer_mdl_ph - outer_mdl_ph[m]))
        patter = (np.abs(cot_meas) <= COT_THRESHOLD) & (np.abs(cot_model) <= COT_THRESHOLD)
        diag = np.concatenate((np.square(outer_meas_err.values), outer_elmts.loc[:]["dK1"],
                               outer_elmts.loc[:]["dX"], outer_elmts.loc[:]["KdS"],
                               outer_elmts.loc[:]["mKdS"]))
        outer_elmts = outer_elmts.rename(columns={"BET" + plane: "BETA"})
        index_tuples = [[x, y] for x in loc_range[patter] + m for y in loc_range[patter] + m
                        if (x < y) and (abs(cot_model[x] - cot_model[y]) > ZERO_THRESHOLD) and
                        (np.sign(cot_model[x] - cot_model[y]) * np.sign(cot_meas[x] - cot_meas[y]) > 0)]
        mat_t_beta, mat_t_alpha = np.zeros((len(index_tuples), len(diag))), np.zeros((len(index_tuples), len(diag)))
        betas, alphas = np.empty(len(index_tuples)), np.empty(len(index_tuples))
        for i, c in enumerate(index_tuples):
            betas[i], alphas[i], mat_t_beta[i], mat_t_alpha[i] = \
                calculate_beta_alpha_from_single_combination(c, sin_squared_elements, outer_elmts, cot_model,
                                                             cot_meas, outer_meas_phase_adv, probed_bpm_name,
                                                             bk_model.at[probed_bpm_name, "BET" + plane],
                                                             bk_model.at[probed_bpm_name, "ALF" + plane], range_of_bpms)

        mask = diag != 0
        mat_diag = np.diag(diag[mask])
        mat_v_beta = np.dot(mat_t_beta[:, mask], np.dot(mat_diag, np.transpose(mat_t_beta[:, mask])))
        mat_v_alpha = np.dot(mat_t_alpha[:, mask], np.dot(mat_diag, np.transpose(mat_t_alpha[:, mask])))

        if np.any(mat_v_beta) and np.any(mat_v_alpha):
            beti, beterr = _covariant_weighting(mat_v_beta, betas)
            alfi, alferr = _covariant_weighting(mat_v_alpha, alphas)
            n_comb[indx] = len(betas)
        else:
            LOGGER.debug(f"ValueError or no combinations left at {probed_bpm_name}.")
            LOGGER.debug(f"betas:\n{betas}")
            LOGGER.debug(f"alphas:\n{alphas}")
            continue
        betas_alfas[indx, :] = np.array([beti, beterr, alfi, alferr])

    beta_df[f"BET{plane}"] = betas_alfas[:, 0]
    beta_df[f"{ERR}BET{plane}"] = betas_alfas[:, 1]
    beta_df[f"ALF{plane}"] = betas_alfas[:, 2]
    beta_df[f"{ERR}ALF{plane}"] = betas_alfas[:, 3]
    beta_df["NCOMB"] = n_comb
    beta_df = beta_df.loc[beta_df["NCOMB"] > 0]
    return beta_df


def calculate_beta_alpha_from_single_combination(c, sin_squared_elements, outer_elmts, cot_model,
                                                 cot_meas, outer_meas_phase_adv, probed_bpm_name,
                                                 betmdl1, alfmdl1, range_of_bpms):
    """
    Calculates beta and alpha function as well as the respective covariance matrix lines for the
    given BPM combination (triplet)
    Args:
        c: relative indices of other two BPMs wrt probed one
        sin_squared_elements:
        outer_elmts:
        cot_model:
        cot_meas:
        outer_meas_phase_adv:
        probed_bpm_name:
        betmdl1:
        alfmdl1:
        range_of_bpms:

    Returns:

    """
    m = int(range_of_bpms / 2)
    ix = c[0]
    iy = c[1]
    fac1, fac2 = -np.sign(c[0]-m), np.sign(c[1]-m)
    dif_cot_model = cot_model[ix] - cot_model[iy]
    # calculate beta
    dif_cot_meas = cot_meas[ix] - cot_meas[iy]
    denom = dif_cot_model / betmdl1
    beta_i = dif_cot_meas / denom
    avg_cot_model = (cot_model[ix] + cot_model[iy]) / 2
    denomalf = 2 * (avg_cot_model + alfmdl1)
    avg_cot_meas = (cot_meas[ix] + cot_meas[iy]) / 2

    alfa_i = 0.5 * (denomalf * dif_cot_meas / dif_cot_model - 2 * avg_cot_meas)

    lng = len(outer_elmts)
    line_length = 4 * len(outer_elmts.index) + 2 * m + 1
    outer_elmts_bet = outer_elmts.loc[:, "BETA"].values
    outer_el_k2 = outer_elmts.loc[:, "K2L"].values
    betaline = np.zeros(line_length)
    alfaline = np.zeros(line_length)

    # slice
    mloc = outer_elmts.index.get_loc(probed_bpm_name)
    xloc_r = outer_elmts.index.get_loc(outer_meas_phase_adv.index[ix])
    yloc_r = outer_elmts.index.get_loc(outer_meas_phase_adv.index[iy])
    denom_sinx = sin_squared_elements[xloc_r, m]
    denom_siny = sin_squared_elements[yloc_r, m]
    xmloc1, xmloc2 = min(xloc_r, mloc), max(xloc_r, mloc)
    ymloc1, ymloc2 = min(yloc_r, mloc), max(yloc_r, mloc)

    # get betas and sin for the elements in the slice
    elem_ph_xa = sin_squared_elements[xmloc1:xmloc2, ix]
    elem_ph_ya = sin_squared_elements[ymloc1:ymloc2, iy]
    elem_beta_xa = outer_elmts_bet[xmloc1:xmloc2]
    elem_beta_ya = outer_elmts_bet[ymloc1:ymloc2]
    elem_k2_xa = outer_el_k2[xmloc1:xmloc2]
    elem_k2_ya = outer_el_k2[ymloc1:ymloc2]

    bet_sin_ix = elem_ph_xa * elem_beta_xa / (denom_sinx * denom)
    bet_sin_iy = elem_ph_ya * elem_beta_ya / (denom_siny * denom)

    off1 = range_of_bpms
    off2 = range_of_bpms + lng
    off3 = range_of_bpms + 2 * lng
    off4 = range_of_bpms + 3 * lng

    # apply phase uncertainty
    betaline[ix] = -1 / (denom_sinx * denom)
    betaline[iy] = 1 / (denom_siny * denom)
    # apply quadrupolar field uncertainty (quadrupole longitudinal misalignment already included)
    betaline[xmloc1 + off1:xmloc2 + off1] += fac1 * bet_sin_ix
    betaline[ymloc1 + off1:ymloc2 + off1] += fac2 * bet_sin_iy
    # apply sextupole transverse misalignment
    betaline[xmloc1 + off2: xmloc2 + off2] += fac1 * elem_k2_xa * bet_sin_ix
    betaline[ymloc1 + off2: ymloc2 + off2] += fac2 * elem_k2_ya * bet_sin_iy
    # apply quadrupole longitudinal misalignments
    betaline[xmloc1 + off3: xmloc2 + off3] += fac1 * bet_sin_ix
    betaline[ymloc1 + off3: ymloc2 + off3] += fac2 * bet_sin_iy
    betaline[xmloc1 + off4: xmloc2 + off4] -= fac1 * bet_sin_ix
    betaline[ymloc1 + off4: ymloc2 + off4] -= fac2 * bet_sin_iy
    # apply phase uncertainty
    alfaline[ix] = -1 / (denom_sinx * denom * betmdl1) * denomalf + 1 / denom_sinx
    alfaline[iy] = 1 / (denom_siny * denom * betmdl1) * denomalf + 1 / denom_siny
    # apply quadrupolar field uncertainty (quadrupole longitudinal misalignment already included)
    alfaline[xmloc1 + off1:xmloc2 + off1] += fac1 * (.5 * (bet_sin_ix * denomalf + bet_sin_ix / betmdl1 * dif_cot_meas))
    alfaline[ymloc1 + off1:ymloc2 + off1] += fac2 * (.5 * (bet_sin_iy * denomalf + bet_sin_iy / betmdl1 * dif_cot_meas))
    # apply sextupole transverse misalignment
    alfaline[xmloc1 + off2: xmloc2 + off2] += fac1 * elem_k2_xa * bet_sin_ix
    alfaline[ymloc1 + off2: ymloc2 + off2] += fac2 * elem_k2_ya * bet_sin_iy
    # apply quadrupole longitudinal misalignments
    alfaline[xmloc1 + off3: xmloc2 + off3] += fac1 * (.5 * elem_k2_xa * (bet_sin_ix * denomalf + bet_sin_ix / betmdl1 * dif_cot_meas))
    alfaline[ymloc1 + off3: ymloc2 + off3] += fac2 * (.5 * elem_k2_ya * (bet_sin_iy * denomalf + bet_sin_iy / betmdl1 * dif_cot_meas))
    alfaline[xmloc1 + off4: xmloc2 + off4] -= fac1 * (.5 * (bet_sin_ix * denomalf + bet_sin_ix / betmdl1 * dif_cot_meas))
    alfaline[ymloc1 + off4: ymloc2 + off4] -= fac2 * (.5 * (bet_sin_iy * denomalf + bet_sin_iy / betmdl1 * dif_cot_meas))

    return beta_i, alfa_i, betaline, alfaline


def _covariant_weighting(mat, col):
    mat_inv = np.linalg.pinv(mat, rcond=RCOND)
    wb = np.sum(mat_inv, axis=1)
    mat_inv_sum = np.sum(wb)
    if mat_inv_sum == 0:
        raise ValueError
    # returns value and error
    return float(np.dot(wb.T, col) / mat_inv_sum), np.sqrt(np.dot(wb.T, np.dot(mat, wb)) / mat_inv_sum ** 2)


def _assign_uncertainties(twiss_full, errordefspath):
    """
    Adds uncertainty information to twiss_full.
    Sources of Errors:
        dK1:    quadrupolar field errors
        dS:     quadrupole longitudinal misalignments
        dX:     sextupole transverse misalignments
        BPMdS:  BPM longitudinal misalignments
    """
    LOGGER.debug("Start creating uncertainty information")
    errdefs = tfs.read(errordefspath)
    twiss_full = twiss_full.assign(UNC=False, dK1=0, KdS=0, mKdS=0, dX=0, BPMdS=0)
    # loop over uncertainty definitions, fill the respective columns, set UNC to true
    for indx in errdefs.index:
        patt = errdefs.loc[indx, "PATTERN"]
        if patt.startswith("key:"):
            LOGGER.debug(f"creating uncertainty information for {patt}")
            mask = patt.split(":")[1]
        else:
            reg = re.compile(patt)
            LOGGER.debug(f"creating uncertainty information for RegEx {patt}")
            mask = twiss_full.index.str.contains(reg)

        twiss_full.loc[mask, "dK1"] = (errdefs.loc[indx, "dK1"] * twiss_full.loc[mask, "K1L"]) ** 2
        twiss_full.loc[mask, "dX"] = errdefs.loc[indx, "dX"]*2
        if errdefs.loc[indx, "MAINFIELD"] == "BPM":
            twiss_full.loc[mask, "BPMdS"] = errdefs.loc[indx, "dS"]**2
        else:
            twiss_full.loc[mask, "KdS"] = (errdefs.loc[indx, "dS"] * twiss_full.loc[mask, "K1L"]) ** 2
        twiss_full.loc[mask, "UNC"] = True

    # in case of quadrupole longitudinal misalignments, the element (DRIFT) in front of the
    # misaligned quadrupole will be used for the thin lens approximation of the misalignment
    twiss_full["mKdS"] = np.roll(twiss_full.loc[:]["KdS"], 1)
    twiss_full.loc[:, "UNC"] = np.logical_or(abs(np.roll(twiss_full.loc[:, "dK1"], -1)) > 1.0e-12,
                                             twiss_full.loc[:, "UNC"])
    LOGGER.debug("DONE creating uncertainty information")
    return twiss_full.loc[twiss_full["UNC"]]


def _get_header(header_dict, error_method, range_of_bpms, rmsbb):
    header = header_dict.copy()
    header['BetaAlgorithmVersion'] = __version__
    header['RCond'] = RCOND
    header['RangeOfBPMs'] = "Adjacent" if error_method == METH_3BPM else range_of_bpms
    header['ErrorsFrom:'] = error_method
    header["RMS_BETABEAT"] = f"{rmsbb:.3f} %"
    return header


def three_bpm_method(phase, plane, meas_and_mdl_tunes, beta_df):
    """
        Calculates betas and alphas from using adjacent BPMs (3 combiantion)

        ``phase["MEAS"]``, ``phase["MODEL"]``, ``phase["ERRMEAS"]`` (from ``get_phases``) are of the
    form:

    +----------+----------+----------+----------+----------+
    |          |   BPM1   |   BPM2   |   BPM3   |   BPM4   |
    +----------+----------+----------+----------+----------+
    |   BPM1   |    0     |  phi_21  |  phi_31  |  phi_41  |
    +----------+----------+----------+----------+----------+
    |   BPM2   |  phi_12  |     0    |  phi_32  |  phi_42  |
    +----------+----------+----------+----------+----------+
    |   BPM3   |  phi_13  |  phi_23  |    0     |  phi_43  |
    +----------+----------+----------+----------+----------+

    aa ``tilt_slice_matrix(matrix, shift, slice, tune)`` brings it into the form:

    +-----------+--------+--------+--------+--------+
    |           |  BPM1  |  BPM2  |  BPM3  |  BPM4  |
    +-----------+--------+--------+--------+--------+
    | BPM_(i-1) | phi_1n | phi_21 | phi_32 | phi_43 |
    +-----------+--------+--------+--------+--------+
    | BPM_i     |    0   |    0   |    0   |    0   |
    +-----------+--------+--------+--------+--------+
    | BPM_(i+1) | phi_12 | phi_23 | phi_34 | phi_45 |
    +-----------+--------+--------+--------+--------+

    ``cot_phase_*_shift1``:

    +-----------------------------+-----------------------------+-----------------------------+
    | cot(phi_1n) - cot(phi_1n-1) |  cot(phi_21) - cot(phi_2n)  |   cot(phi_32) - cot(phi_31) |
    +-----------------------------+-----------------------------+-----------------------------+
    |         NaN                 |         NaN                 |         NaN                 |
    +-----------------------------+-----------------------------+-----------------------------+
    |         NaN                 |         NaN                 |         NaN                 |
    +-----------------------------+-----------------------------+-----------------------------+
    |  cot(phi_13) - cot(phi_12)  |  cot(phi_24) - cot(phi_23)  |   cot(phi_35) - cot(phi_34) |
    +-----------------------------+-----------------------------+-----------------------------+

    for the combination xxxABBx: first row
    for the combinstion xBBAxxx: fourth row and
    for the combination xxBABxx: second row of ``cot_phase_*_shift2``

        Args:
            phase: phase matrices of measurement with errors and model tfs (bpm x bpm)
            plane: plane either X or Y
            meas_and_mdl_tunes: measured  and model tunes
            beta_df: tfs skeleton

        Returns:
            tfs.DataFrame containing betas and alfas from phase
        """
    tune, mdltune = meas_and_mdl_tunes
    # tilt phase advances in order to have the phase advances in a neighbourhood
    tilted_meas = _tilt_slice_matrix(phase["MEAS"].values, 2, 5, tune) * TWOPI
    tilted_model = _tilt_slice_matrix(phase["MODEL"].values, 2, 5, mdltune) * TWOPI
    tilted_errmeas = _tilt_slice_matrix(phase["ERRMEAS"].values, 2, 5, mdltune) * TWOPI
    betmdl = beta_df.loc[:]["BET" + plane + "MDL"].values
    alfmdl = beta_df.loc[:]["ALF" + plane + "MDL"].values
    with np.errstate(divide='ignore'):
        cot_phase_meas = 1 / np.tan(tilted_meas)
        cot_phase_model = 1 / np.tan(tilted_model)
    # calculate enumerators and denominators for far more cases than needed
    # shift1 are the cases BBA, ABB, AxBB, AxxBB etc. (the used BPMs are adjacent)
    # shift2 are the cases where the used BPMs are separated by one. only BAB is used for  3-BPM
    cot_phase_meas_shift1 = cot_phase_meas - np.roll(cot_phase_meas, -1, axis=0)
    cot_phase_model_shift1 = cot_phase_model - np.roll(cot_phase_model, -1, axis=0) + EPSILON
    cot_phase_meas_shift2 = cot_phase_meas - np.roll(cot_phase_meas, -2, axis=0)
    cot_phase_model_shift2 = cot_phase_model - np.roll(cot_phase_model, -2, axis=0) + EPSILON
    # calculate the sum of the fractions
    bet_frac = (cot_phase_meas_shift1[0]/cot_phase_model_shift1[0] +
                cot_phase_meas_shift1[3]/cot_phase_model_shift1[3] +
                cot_phase_meas_shift2[1]/cot_phase_model_shift2[1]) / 3
    # multiply the fractions by betmdl and calculate the arithmetic mean
    beti = bet_frac * betmdl
    alfi = (bet_frac * (cot_phase_model[1] + cot_phase_model[3] + 2 * alfmdl) - (cot_phase_meas[1] + cot_phase_meas[3])) / 2
    # calculate errphi_ij^2 / sin^2 phimdl_ij * beta
    with np.errstate(divide='ignore', invalid='ignore'):
        sin_squared_model = tilted_errmeas / np.square(np.sin(tilted_model)) * betmdl
    # square it again beacause it's used in a vector length
    sin_squared_model = np.square(sin_squared_model)
    sin_squ_model_shift1 = sin_squared_model + np.roll(sin_squared_model, -1, axis=0) / np.square(cot_phase_model_shift1)
    sin_squ_model_shift2 = sin_squared_model + np.roll(sin_squared_model, -2, axis=0) / np.square(cot_phase_model_shift2)
    beterr = np.sqrt(sin_squ_model_shift1[0] + sin_squ_model_shift1[3] + sin_squ_model_shift2[1]) / 3
    beta_df["BET" + plane] = beti
    beta_df["ERRBET" + plane] = beterr
    beta_df["ALF" + plane] = alfi
    beta_df["ERRALF" + plane] = 0  # TODO calculate alferr
    return beta_df


def _tilt_slice_matrix(matrix, slice_shift, slice_width, tune=0):
    """
    Tilts and slices the ``matrix``
    Tilting means shifting each column upwards one step more than the previous columnns, i.e.

    a a a a a       a b c d
    b b b b b       b c d e
    c c c c c  -->  c d e f
    ...             ...
    y y y y y       y z a b
    z z z z z       z a b c
    """
    invrange = matrix.shape[0] - 1 - np.arange(matrix.shape[0])
    matrix[matrix.shape[0] - slice_shift:, :slice_shift] += tune
    matrix[:slice_shift, matrix.shape[1] - slice_shift:] -= tune
    return np.roll(matrix[np.arange(matrix.shape[0]), circulant(invrange)[invrange]],
                   slice_shift, axis=0)[:slice_width]
