"""
Module tune_analysis.detuning_tools
-------------------------------------

Some tools for amplitude detuning, mainly plotting.

Important Convention:
    The beta-parameter in the ODR models go upwards with order, i.e.
    |  beta[0] = y-Axis offset
    |  beta[1] = slope
    |  beta[2] = quadratic term
    |  etc.

"""
import numpy as np
from scipy.odr import RealData, Model, ODR

from utils import logging_tools

LOG = logging_tools.get_logger(__name__)


# ODR ###################################################################


def get_poly_fun(order):
    """ Returns the function of polynomial order. (is this pythonic enough?)"""
    def poly_func(beta, x):
        return sum(beta[i] * np.power(x, i) for i in range(order+1))
    return poly_func


def do_odr(x, y, xerr, yerr, order):
    """ Returns the odr fit.

    Args:
        x: Series of x data
        y: Series of y data
        xerr: Series of x data errors
        yerr: Series of y data errors
        order: fit order; 1: linear, 2: quadratic

    Returns: Odr fit. Betas order is index = coefficient of same order.
             See :func:`omc3.tune_analysis.detuning_tools.linear_model`
             and :func:`omc3.tune_analysis.detuning_tools.quadratic_model`.
    """
    odr = ODR(data=RealData(x, y, xerr, yerr),
              model=Model(get_poly_fun(order)),
              beta0=[0.] + [1.] * order)
    odr_fit = odr.run()
    logging_tools.odr_pprint(LOG.info, odr_fit)
    return odr_fit

