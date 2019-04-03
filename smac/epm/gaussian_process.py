import logging
import typing

import numpy as np
import skopt.learning.gaussian_process
import skopt.learning.gaussian_process.kernels
from scipy import optimize

from smac.epm.base_gp import BaseModel
from smac.epm.gp_base_prior import Prior
from smac.utils.constants import VERY_SMALL_NUMBER

logger = logging.getLogger(__name__)


class GaussianProcess(BaseModel):
    """
    Gaussian process model.

    The GP hyperparameterŝ are obtained by optimizing the marginal log likelihood.

    This code is based on the implementation of RoBO:

    Klein, A. and Falkner, S. and Mansur, N. and Hutter, F.
    RoBO: A Flexible and Robust Bayesian Optimization Framework in Python
    In: NIPS 2017 Bayesian Optimization Workshop

    Parameters
    ----------
    types : np.ndarray (D)
        Specifies the number of categorical values of an input dimension where
        the i-th entry corresponds to the i-th input dimension. Let's say we
        have 2 dimension where the first dimension consists of 3 different
        categorical choices and the second dimension is continuous than we
        have to pass np.array([2, 0]). Note that we count starting from 0.
    bounds : list
        Specifies the bounds for continuous features.
    seed : int
        Model seed.
    kernel : george kernel object
        Specifies the kernel that is used for all Gaussian Process
    prior : prior object
        Defines a prior for the hyperparameters of the GP. Make sure that
        it implements the Prior interface.
    normalize_y : bool
        Zero mean unit variance normalization of the output values
    rng: np.random.RandomState
        Random number generator
    """

    def __init__(
        self,
        types: np.ndarray,
        bounds: typing.List[typing.Tuple[float, float]],
        seed: int,
        kernel: skopt.learning.gaussian_process.kernels.Kernel,
        prior: Prior=None,
        normalize_y: bool=True,
        **kwargs
    ):

        super().__init__(types=types, bounds=bounds, seed=seed, **kwargs)

        self.kernel = kernel
        self.gp = None
        self.prior = prior
        self.normalize_y = normalize_y
        self.X = None
        self.y = None
        self.hypers = []
        self.is_trained = False

    def _train(self, X: np.ndarray, y: np.ndarray, do_optimize: bool=True):
        """
        Computes the Cholesky decomposition of the covariance of X and
        estimates the GP hyperparameters by optimizing the marginal
        loglikelihood. The prior mean of the GP is set to the empirical
        mean of X.

        Parameters
        ----------
        X: np.ndarray (N, D)
            Input data points. The dimensionality of X is (N, D),
            with N as the number of points and D is the number of features.
        y: np.ndarray (N,)
            The corresponding target values.
        do_optimize: boolean
            If set to true the hyperparameters are optimized otherwise
            the default hyperparameters of the kernel are used.
        """

        if self.normalize_y:
            y = self._normalize_y(y)

        self.gp = skopt.learning.gaussian_process.GaussianProcessRegressor(
            kernel=self.kernel,
            normalize_y=False,
            optimizer=None,
            n_restarts_optimizer=-1,  # Do not use scikit-learn's optimization routine
            alpha=0,  # Governed by the kernel
            noise=None,
        )
        # Initialize some variables
        self.gp.fit(X, y)

        if do_optimize:
            self.hypers = self._optimize()
        else:
            self.hypers = self.gp.kernel.theta

        self.gp.kernel.theta = self.hypers
        self.gp.fit(X, y)

        self.is_trained = True

    def _nll(self, theta: np.ndarray) -> typing.Tuple[float, np.ndarray]:
        """
        Returns the negative marginal log likelihood (+ the prior) for
        a hyperparameter configuration theta.
        (negative because we use scipy minimize for optimization)

        Parameters
        ----------
        theta : np.ndarray(H)
            Hyperparameter vector. Note that all hyperparameter are
            on a log scale.

        Returns
        ----------
        float
            lnlikelihood + prior
        """

        lml, grad = self.gp.log_marginal_likelihood(theta, eval_gradient=True)

        # Add prior
        if self.prior is not None:
            lml += self.prior.lnprob(theta)
            grad += self.prior.gradient(theta)

        # We add a minus here because scipy is minimizing
        if not np.isfinite(lml).all() or not np.all(np.isfinite(grad)):
            return 1e25, np.array([1e25] * theta.shape[0])
        else:
            return -lml, -grad

    def _optimize(self) -> np.ndarray:
        """
        Optimizes the marginal log likelihood and returns the best found
        hyperparameter configuration theta.

        Returns
        -------
        theta : np.ndarray(H)
            Hyperparameter vector that maximizes the marginal log likelihood
        """
        # Start optimization from the previous hyperparameter configuration
        p0 = self.gp.kernel.theta
        bounds = [(np.exp(b[0]), np.exp(b[1])) for b in self.gp.kernel.bounds]
        theta, f_opt, _ = optimize.fmin_l_bfgs_b(self._nll, p0, bounds=bounds)
        return theta

    def _predict(self, X_test: np.ndarray, full_cov: bool=False):
        r"""
        Returns the predictive mean and variance of the objective function at
        the given test points.

        Parameters
        ----------
        X_test: np.ndarray (N, D)
            Input test points
        full_cov: bool
            If set to true than the whole covariance matrix between the test points is returned

        Returns
        ----------
        np.array(N,)
            predictive mean
        np.array(N,) or np.array(N, N) if full_cov == True
            predictive variance

        """

        if not self.is_trained:
            raise Exception('Model has to be trained first!')

        mu, var = self.gp.predict(X_test, return_cov=True)
        var = np.diag(var)
        if self.normalize_y:
            mu, var = self._untransform_y(mu, var)

        # Clip negative variances and set them to the smallest
        # positive float value
        np.clip(var, VERY_SMALL_NUMBER, np.inf)

        return mu, var

    def sample_functions(self, X_test: np.ndarray, n_funcs: int=1) -> np.ndarray:
        """
        Samples F function values from the current posterior at the N
        specified test points.

        Parameters
        ----------
        X_test: np.ndarray (N, D)
            Input test points
        n_funcs: int
            Number of function values that are drawn at each test point.

        Returns
        ----------
        function_samples: np.array(F, N)
            The F function values drawn at the N test points.
        """

        if not self.is_trained:
            raise Exception('Model has to be trained first!')

        funcs = self.gp.sample_y(X_test, n_samples=n_funcs, random_state=self.rng)
        funcs = np.squeeze(funcs, axis=1)

        if self.normalize_y:
            funcs = self._untransform_y(funcs)

        if len(funcs.shape) == 1:
            return funcs[None, :]
        else:
            return funcs
