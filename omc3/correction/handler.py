import datetime
import os
import pickle
import time

import numpy as np
import pandas as pd
from sklearn.linear_model import OrthogonalMatchingPursuit

import madx_wrapper
import tfs
from correction import filters, model_appenders, response_twiss, optics_class
from correction.constants import DELTA, DIFF, WEIGHT, VALUE, ERROR
from optics_measurements.constants import EXT, PHASE_NAME, DISPERSION_NAME, NORM_DISP_NAME
from utils import logging_tools
LOG = logging_tools.get_logger(__name__)


def correct(accel_inst, opt):
    """

    Args:
        accel_opt:
        opt:

    Returns:

    """
    meth_opt = _get_method_opt(opt)
    # read data from files
    vars_list = _get_varlist(accel_inst, opt.variable_categories, opt.virt_flag)
    optics_params, meas_dict = _get_measurment_data(opt.optics_params, opt.meas_dir,
                                                    opt.beta_file_name, opt.weights, )
    if opt.fullresponse_path is not None:
        resp_dict = _load_fullresponse(opt.fullresponse_path, vars_list)
    else:
       resp_dict = response_twiss.create_response(accel_inst, opt.variable_categories,
                                                  optics_params)
    # the model in accel_inst is modified later, so save nominal model here to variables
    nominal_model = _maybe_add_coupling_to_model(accel_inst.model, optics_params)
    # apply filters to data
    meas_dict = filters.filter_measurement(optics_params, meas_dict, nominal_model, opt)
    meas_dict = model_appenders.append_model_to_measurement(nominal_model, meas_dict, optics_params)
    resp_dict = filters.filter_response_index(resp_dict, meas_dict, optics_params)
    resp_matrix = _join_responses(resp_dict, optics_params, vars_list)
    delta = tfs.TfsDataFrame(0, index=vars_list, columns=[DELTA])
    # ######### Iteration Phase ######### #
    for iteration in range(opt.max_iter + 1):
        LOG.info(f"Correction Iteration {iteration} of {opt.max_iter}.")

        # ######### Update Model and Response ######### #
        if iteration > 0:
            LOG.debug("Updating model via MADX.")
            corr_model_path = os.path.join(opt.output_dir, f"twiss_{iteration}{EXT}")
            _create_corrected_model(corr_model_path, opt.change_params_path, accel_inst)

            corr_model_elements = tfs.read(corr_model_path, index="NAME")
            corr_model_elements = _maybe_add_coupling_to_model(corr_model_elements, optics_params)

            bpms_index_mask = accel_inst.get_element_types_mask(corr_model_elements.index,
                                                                types=["bpm"])
            corr_model = corr_model_elements.loc[bpms_index_mask, :]

            meas_dict = model_appenders.append_model_to_measurement(corr_model, meas_dict,
                                                                    optics_params)
            if opt.update_response:
                LOG.debug("Updating response.")
                # please look away for the next two lines.
                accel_inst._model = corr_model
                accel_inst._elements = corr_model_elements
                resp_dict = response_twiss.create_response(accel_inst, opt.variable_categories,
                                                           optics_params)
                resp_dict = filters.filter_response_index(resp_dict, meas_dict, optics_params)
                resp_matrix = _join_responses(resp_dict, optics_params, vars_list)

        # ######### Actual optimization ######### #
        delta += _calculate_delta(resp_matrix, meas_dict, optics_params, vars_list, opt.method,
                                  meth_opt)
        delta, resp_matrix, vars_list = _filter_by_strength(delta, resp_matrix,
                                                            opt.min_corrector_strength)
        # remove unused correctors from vars_list

        writeparams(opt.change_params_path, delta)
        writeparams(opt.change_params_correct_path, -delta)
        LOG.debug(f"Cumulative delta: {np.sum(np.abs(delta.loc[:, DELTA].values)):.5e}")
    write_knob(opt.knob_path, delta)
    LOG.info("Finished Iterative Global Correction.")


def _get_method_opt(opt):
    """ Slightly unnecessary function to separate method-options
    for easier debugging and readability """
    return opt.get_subdict(["svd_cut", "n_correctors"])


def _print_rms(meas, diff_w, r_delta_w):
    """ Prints current RMS status """
    f_str = "{:>20s} : {:.5e}"
    LOG.debug("RMS Measure - Model (before correction, w/o weigths):")
    for key in meas:
        LOG.debug(f_str.format(key, _rms(meas[key].loc[:, DIFF].values)))

    LOG.info("RMS Measure - Model (before correction, w/ weigths):")
    for key in meas:
        LOG.info(f_str.format(
            key, _rms(meas[key].loc[:, DIFF].values * meas[key].loc[:, WEIGHT].values)))

    LOG.info(f_str.format("All", _rms(diff_w)))
    LOG.debug(f_str.format("R * delta", _rms(r_delta_w)))
    LOG.debug("(Measure - Model) - (R * delta)   ")
    LOG.debug(f_str.format("", _rms(diff_w - r_delta_w)))


def _load_fullresponse(full_response_path, variables):
    """
    Full response is dictionary of optics-parameter gradients upon
    a change of a single quadrupole strength
    """
    LOG.debug("Starting loading Full Response optics")
    with open(full_response_path, "rb") as full_response_file:
        full_response_data = pickle.load(full_response_file)
    loaded_vars = [var for resp in full_response_data.values() for var in resp]
    if not any([v in loaded_vars for v in variables]):
        raise ValueError("None of the given variables found in response matrix. "
                         "Are you using the right categories?")

    LOG.debug("Loading ended")
    return full_response_data


def _get_measurment_data(keys, meas_dir, beta_file_name, w_dict):
    """ Retruns a dictionary full of get_llm data """
    measurement = {}
    filtered_keys = [k for k in keys if w_dict[k] != 0]
    for key in filtered_keys:
        if key.startswith('MU'):
            measurement[key] = read_meas(meas_dir, f"{PHASE_NAME}{key[-1].lower()}{EXT}")
        elif key.startswith('D'):
            measurement[key] = read_meas(meas_dir, f"{DISPERSION_NAME}{key[-1].lower()}{EXT}")
        elif key == "NDX":
            measurement[key] = read_meas(meas_dir, f"{NORM_DISP_NAME}{key[-1].lower()}{EXT}")
        elif key in ('F1001R', 'F1001I', 'F1010R', 'F1010I'):
            pass  # TODO now it doesn't load coupling files
        elif key == "Q":
            measurement[key] = pd.DataFrame({
                # Just fractional tunes:
                VALUE: np.remainder([read_meas(meas_dir, f"{PHASE_NAME}x{EXT}")['Q1'],
                                     read_meas(meas_dir, f"{PHASE_NAME}x{EXT}")['Q2']], [1, 1]),
                # TODO measured errors not in the file
                ERROR: np.array([0.001, 0.001])
            }, index=['Q1', 'Q2'])
        elif key.startswith('BET'):
            measurement[key] = read_meas(meas_dir, f"{beta_file_name}{key[-1].lower()}{EXT}")
    return filtered_keys, measurement


def read_meas(meas_dir, filename):
    return tfs.read(os.path.join(meas_dir, filename), index="NAME")


def _get_varlist(accel_cls, variables, virt_flag):  # TODO: Virtual?
    varlist = np.array(accel_cls.get_variables(classes=variables))
    if len(varlist) == 0:
        raise ValueError("No variables found! Make sure your categories are valid!")
    return varlist


def _maybe_add_coupling_to_model(model, keys):
    if any([key for key in keys if key.startswith("F1")]):
        couple = optics_class.get_coupling(model)
        model["F1001R"] = couple["F1001"].apply(np.real).astype(np.float64)
        model["F1001I"] = couple["F1001"].apply(np.imag).astype(np.float64)
        model["F1010R"] = couple["F1010"].apply(np.real).astype(np.float64)
        model["F1010I"] = couple["F1010"].apply(np.imag).astype(np.float64)
    return model


def _calculate_delta(resp_matrix, meas_dict, keys, vars_list, method, meth_opt):
    """ Get the deltas for the variables.

    Output is Dataframe with one column 'DELTA' and vars_list index. """
    weight_vector = _join_columns('WEIGHT', meas_dict, keys)
    diff_vector = _join_columns('DIFF', meas_dict, keys)

    resp_weighted = resp_matrix.mul(weight_vector, axis="index")
    diff_weighted = diff_vector * weight_vector

    delta = _get_method_fun(method)(resp_weighted, diff_weighted, meth_opt)
    delta = tfs.TfsDataFrame(delta, index=vars_list, columns=[DELTA])

    # check calculations
    update = np.dot(resp_weighted, delta[DELTA])
    _print_rms(meas_dict, diff_weighted, update)

    return delta


def _get_method_fun(method):
    funcs = {"pinv": _pseudo_inverse, "omp": _orthogonal_matching_pursuit,}
    return funcs[method]


def _pseudo_inverse(response_mat, diff_vec, opt):
    """ Calculates the pseudo-inverse of the response via svd. (numpy) """
    if opt.svd_cut is None:
        raise ValueError("svd_cut setting needed for pseudo inverse method.")

    return np.dot(np.linalg.pinv(response_mat, opt.svd_cut), diff_vec)


def _orthogonal_matching_pursuit(response_mat, diff_vec, opt):
    """ Calculated n_correctors via orthogonal matching pursuit"""
    if opt.n_correctors is None:
        raise ValueError("n_correctors setting needed for orthogonal matching pursuit.")

    # return orthogonal_mp(response_mat, diff_vec, opt.n_correctors)
    res = OrthogonalMatchingPursuit(opt.n_correctors).fit(response_mat, diff_vec)
    coef = res.coef_
    LOG.debug(f"Orthogonal Matching Pursuit Results: \n"
              f"  Chosen variables: {response_mat.columns.values[coef.nonzero()]}\n"
              f"  Score: {res.score(response_mat, diff_vec)}")
    return coef


def _create_corrected_model(twiss_out, change_params, accel_inst):
    """ Use the calculated deltas in changeparameters.madx to create a corrected model """
    madx_script = accel_inst.get_update_correction_script(twiss_out, change_params)
    madx_wrapper.run_string(madx_script, log_file=os.devnull, )


def write_knob(knob_path, delta):
    a = datetime.datetime.fromtimestamp(time.time())
    delta_out = - delta.loc[:, [DELTA]]
    delta_out.headers["PATH"] = os.path.dirname(knob_path)
    delta_out.headers["DATE"] = str(a.ctime())
    tfs.write(knob_path, delta_out, save_index="NAME")


def writeparams(path_to_file, delta):
    with open(path_to_file, "w") as madx_script:
        for var in delta.index.values:
            value = delta.loc[var, DELTA]
            madx_script.write(f"{var} = {var} {value:+e};\n")


def _rms(a):
    return np.sqrt(np.mean(np.square(a)))


def _join_responses(resp, keys, varslist):
    """ Returns matrix #BPMs * #Parameters x #variables """
    return pd.concat([resp[k] for k in keys],  # dataframes
                     axis="index",  # axis to join along
                     join_axes=[pd.Index(varslist)]
                     # other axes to use (pd Index obj required)
                     ).fillna(0.0)


def _join_columns(col, meas, keys):
    """ Retuns vector: N= #BPMs * #Parameters (BBX, MUX etc.) """
    return np.concatenate([meas[key].loc[:, col].values for key in keys], axis=0)


def _filter_by_strength(delta, resp_matrix, min_strength=0):
    """ Remove too small correctors """
    delta = delta.loc[delta[DELTA].abs() > min_strength]
    return delta, resp_matrix.loc[:, delta.index], delta.index.values