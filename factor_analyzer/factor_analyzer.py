"""
Factor analysis using MINRES -OR- ML,
with optional rotation using Varimax or Promax.

:author: Jeremy Biggs (jbiggs@ets.org)

:date: 10/25/2017
:organization: ETS
"""

import os
import argparse
import logging

import numpy as np
import scipy as sp
import pandas as pd

from scipy.optimize import minimize
from sklearn.preprocessing import scale
from sklearn.linear_model import LinearRegression


def read_file(file_path):
    """
    A helper function to read file in
    CSV, TSV, or XLXS format into a data_frame.

    Parameters
    ----------
    file_path : str
        The path to a CSV, TSV, or XLSX file.


    Returns
    -------
    data : pd.DataFrame
        The data file read into a pandas data_frame.

    Raises
    ------
    ValueError
        If the file is not CSV, TSV, or XLSX.
    """
    # read file in format CSV, TSV, or XLSX
    if file_path.lower().endswith('.csv'):
        data = pd.read_csv(file_path)
    elif file_path.lower().endswith('.tsv'):
        data = pd.read_csv(file_path, sep='\t')
    elif file_path.lower().endswith('.xlsx'):
        data = pd.read_excel(file_path)
    else:
        raise ValueError('The file must be either CSV, TSV, or XLSX format. '
                         'You have specified the following : {}'.format(file_path))
    return data


def calculate_bartlett_sphericity(data):
    """
    Test the hypothesis that the correlation matrix
    is equal to the identity matrix.identity

    H0: The matrix of population correlations is equal to I.
    H1: The matrix of population correlations is not equal to I.

    The formula for Bartlett's Sphericity test is:

    X^2 = -1 * (n - 1 - ((2p + 5) / 6)) * ln(det(R))

    Where R det(R) is the determinant of the correlation matrix,
    and p is the number of variables.

    Parameters
    ----------
    data : pd.DataFrame
        The data to analyze.

    Returns
    -------
    chi_square : float
        The chi-square value
    p_value : float
        The p-value for the test.
    """
    n, p = data.shape

    corr = data.corr()

    chi_square = -(n - 1 - (2 * p + 5) / 6) * np.log(np.linalg.det(corr))
    degrees_of_freedom = p * (p - 1) / 2
    p_value = sp.stats.chi2.pdf(chi_square, degrees_of_freedom)
    return chi_square, p_value


def calculate_kaiser_meyer_olkin(data):
    """
    Calculate the Kaiser-Meter_Olkin measure of sampling adequacy.
    The KMO checks if we can efficiently factorize
    the original variables. KMO returns values between 0 and 1, which
    can generally be interpreted as follows:

    - Values close to zero indicate high partial correlations,
      meaning EFA may be a problem.
    - Values greater than zero and less than 0.5 indicate the sampling
      is not adequate.
    - Values greater than 0.5 and less than 0.8 indicate sampling
      may be adequate.
    - Values between 0.8 and 1 indicate that sampling is adequate.

    Parameters
    ----------
    data : pd.DataFrame
        The data to analyze using KMO.

    Returns
    -------
    kmo_value : float
        The KMO value.
    """

    # get correlation and inverse correlation matrices
    corr = data.corr()
    corr_inv = np.linalg.inv(corr)

    # get number of rows and number of columns
    n, p = corr.shape

    # 1. calculate partial correlation matrix
    A = np.ones((n, p))
    for i in range(n):
        for j in range(i, p):

            A[i, j] = -corr_inv[i, j] / np.sqrt(corr_inv[i, i] * corr_inv[j, j])
            A[j, i] = A[i, j]

    # 2. calculate global KMO value
    kmo_number = np.sum(np.square(corr)) - np.sum(np.square(np.diagonal(corr)))
    kmo_denominator = kmo_number + np.sum(np.square(A)) - np.sum(np.square(np.diagonal(A)))
    kmo_value = kmo_number / kmo_denominator
    return kmo_value


class FactorAnalyzer:
    """
    FactorAnalyzer class, which -

        (1) Fits a factor analysis model using MINRES -OR- Maximum Likelihood,
            and returns the loading matrix

        (2) Optionally performs a rotation on the loading matrix using
            either -

            (a) Varimax (orthogonal rotation) -OR-
            (b) Promax (oblique rotation)

    Adapted from:

    - https://github.com/cran/psych/blob/master/R/fa.R

    - https://github.com/SurajGupta/r-source/blob/master/
      src/library/stats/R/factanal.R
    """

    def __init__(self,
                 log_warnings=False):
        """
        Initialize object.

        Parameters
        ----------
        log_warnings : bool
            Whether to log warnings, such as failure to
            converge.
            Defaults to False.
        """

        self.log_warnings = log_warnings

        # default matrices to None
        self.corr = None
        self.loadings = None
        self.rotation_matrix = None

    def remove_non_numeric(self, data):
        """
        Remove non-numeric columns from data,
        as these columns cannot be used in
        factor analysis.

        Parameters
        ----------
        data : pd.DataFrame
            The dataframe from which to remove
            non-numeric columns.

        Returns
        -------
        data : pd.DataFrame
            The dataframe with non-numeric columns removed.
        """
        old_column_names = data.columns.values
        data = data.loc[:, data.applymap(sp.isreal).all() == True].copy()

        # report any non-numeric columns removed
        non_numeric_columns = set(old_column_names) - set(data.columns)
        if non_numeric_columns and self.log_warnings:
            logging.warning('The following non-numeric columns '
                            'were removed: {}.'.format(', '.join(non_numeric_columns)))
        return data

    @staticmethod
    def smc(data, sort=False):
        """
        Calculate the squared multiple correlations.
        This is equivalent to regressing each variable
        on all others and calculating the r-squared values.

        Parameters
        ----------
        data : pd.DataFrame
            The dataframe used to calculate SMC.
        sort : bool
            Whether to sort the values for SMC
            before returning.
            Defaults to False.

        Returns
        -------
        smc : pd.DataFrame
            The squared multiple correlations matrix.
        """
        corr = data.corr()
        columns = data.columns

        corr_inv = sp.linalg.inv(corr)
        smc = 1 - 1 / sp.diag(corr_inv)

        smc = pd.DataFrame(smc,
                           index=columns,
                           columns=['SMC'])
        if sort:
            smc = smc.sort_values('SMC')
        return smc

    @staticmethod
    def _fit_uls_objective(psi, corr_mtx, n_factors):
        """
        The objective function passed to `minimize()` for ULS.

        Parameters
        ----------
        psi : np.array
            Value passed to minimize the objective function.
        corr_mtx : np.array
            The correlation matrix.
        n_factors : int
            The number of factors to select.

        Returns
        -------
        error : float
            The scalar error calculated from the residuals
            of the loading matrix.
        """
        np.fill_diagonal(corr_mtx, 1 - psi)

        # get the eigen values and vectors for n_factors
        values, vectors = np.linalg.eig(corr_mtx)

        # make sure that only the real part of each eigenvalue
        # is used, if `complex` is returned
        values = np.real(values)

        # this is a bit of a hack, borrowed from R's `fac()` function;
        # if values are smaller than the smallest representable positive
        # number * 100, set them to that number instead.
        values = np.maximum(values, np.finfo(float).eps * 100)

        values = sorted(values, reverse=True)[:n_factors]
        vectors = vectors[:, :n_factors]

        # calculate the loadings
        if n_factors > 1:

            loadings = np.dot(vectors,
                              np.diag(np.sqrt(values)))
        else:
            loadings = vectors * np.sqrt(values[0])

        # calculate the error from the loadings model
        model = sp.dot(loadings, loadings.T)

        # note that in a more recent version of the `fa()` source
        # code on GitHub, the minres objective function only sums the
        # lower triangle of the residual matrix; this could be
        # implemented here using `np.tril()` when this change is
        # merged into the stable version of `psych`.
        residual = (corr_mtx - model)**2
        error = sp.sum(residual)
        return error

    @staticmethod
    def _fit_ml_objective(psi, corr_mtx, n_factors):
        """
        The objective function passed to `minimize()` for ML.

        Parameters
        ----------
        psi : np.array
            Value passed to minimize the objective function.
        corr_mtx : np.array
            The correlation matrix.
        n_factors : int
            The number of factors to select.

        Returns
        -------
        error : float
            The scalar error calculated from the residuals
            of the loading matrix.
        """
        sc = np.diag(1 / np.sqrt(psi))
        sstar = np.dot(np.dot(sc, corr_mtx), sc)

        # get the eigenvalues and eigenvectors for n_factors
        values, _ = np.linalg.eig(sstar)
        values = sorted(values)[:-n_factors][::-1]

        # calculate the error
        error = -(np.sum(np.log(values) - values) -
                  n_factors + corr_mtx.shape[0])
        return error

    @staticmethod
    def _normalize_wls(solution, corr_mtx, n_factors):
        """
        Weighted least squares normalization for loadings
        estimated using MINRES.

        Parameters
        ----------
        solution : np.array
            The solution from the L-BFGS-B optimization.
        corr_mtx : np.array
            The correlation matrix.
        n_factors : int
            The number of factors to select.

        Returns
        -------
        loadings : pd.DataFrame
            The factor loading matrix
        """
        sp.fill_diagonal(corr_mtx, 1 - solution)

        # get the eigenvalues and vectors for n_factors
        values, vectors = sp.linalg.eig(corr_mtx)
        values, vectors = values[:n_factors], vectors[:, :n_factors]

        # make sure that only the real part of the value
        # is used, if `complex` is returned
        values = sp.real(values)

        # calculate loadings
        # if values are smaller than 0, set them to zero
        loadings = sp.dot(vectors, sp.diag(sp.sqrt(np.maximum(values, 0))))
        return loadings

    @staticmethod
    def _normalize_ml(solution, corr_mtx, n_factors):
        """
        Normalization for loadings estimated using ML.

        Parameters
        ----------
        solution : np.array
            The solution from the L-BFGS-B optimization.
        corr_mtx : np.array
            The correlation matrix.
        n_factors : int
            The number of factors to select.

        Returns
        -------
        loadings : pd.DataFrame
            The factor loading matrix
        """
        sc = np.diag(1 / np.sqrt(solution))
        sstar = np.dot(np.dot(sc, corr_mtx), sc)

        # get the eigenvalues for n_factors
        values, vectors = np.linalg.eig(sstar)
        values = np.array(sorted(values, reverse=True)[:n_factors])
        values = np.maximum(values - 1, 0)

        # get the eigenvectors for n_factors
        vectors = vectors[:, :n_factors]

        # get the loadings
        loadings = np.dot(vectors,
                          np.diag(np.sqrt(values)))

        return np.dot(np.diag(np.sqrt(solution)), loadings)

    def fit_factor_analysis(self,
                            data,
                            n_factors,
                            use_smc=True,
                            bounds=(0.005, 1),
                            method='minres'):
        """
        Fit the factor analysis model.

        Parameters
        ----------
        data : pd.DataFrame
            The data to fit.
        n_factors : int
            The number of factors to select.
        use_smc : bool
            Whether to use squared multiple correlation
            as starting guesses for factor analysis.
            Defaults to True.
        bounds : tuple
            The lower and upper bounds on the variables
            for "L-BFGS-B" optimization.
            Defaults to (0.005, 1).
        method : {'minres', 'ml'}
            The fitting method to use, either MINRES or
            Maximum Likelihood.
            Defaults to 'minres'.

        Returns
        -------
        loadings : pd.DataFrame
            The factor loadings matrix

        Raises
        ------
        ValueError
            If any of the correlations are null, most likely due
            to having zero standard deviation.
        """

        if method not in ['ml', 'minres'] and self.log_warnings:
            logging.warning("You have selected a method other than 'minres' or 'ml'. "
                            "MINRES will be used by default, as {} is not a valid "
                            "option.".format(method))

        corr = data.corr()

        # if any variables have zero standard deviation, then
        # the correlation will be NaN, as you cannot divide by zero:
        # corr(i,j ) = cov(i, j) / (stdev(i) * stdev(j))

        if corr.isnull().any().any():
            raise ValueError('The correlation matrix cannot have '
                             'features that are null or infinite. '
                             'Check to make sure you do not have any '
                             'features with zero standard deviation.')

        corr = corr.as_matrix()

        # if `use_smc` is True, get get squared multiple correlations
        # and use these as initial guesses for optimizer
        if use_smc:
            smc_mtx = self.smc(data).as_matrix()
            start = (np.diag(corr) - smc_mtx.T).squeeze()

        # otherwise, just start with a guess of 0.5 for everything
        else:
            start = [0.5 for _ in range(corr.shape[0])]

        # if `bounds`, set initial boundaries for all variables;
        # this must be a list passed to `minimize()`
        if bounds is not None:
            bounds = [bounds for _ in range(corr.shape[0])]

        # minimize the appropriate objective function
        # and the L-BFGS-B algorithm
        if method == 'ml':
            objective = self._fit_ml_objective
        else:
            objective = self._fit_uls_objective

        res = minimize(objective,
                       start,
                       method='L-BFGS-B',
                       bounds=bounds,
                       options={'maxiter': 1000},
                       args=(corr, n_factors))

        if not res.success and self.log_warnings:
            logging.warning('Failed to converge: {}'.format(res.message))

        # get factor column names
        columns = ['Factor{}'.format(i) for i in range(1, n_factors + 1)]

        # transform the final loading matrix (using wls for MINRES,
        # and ml normalization for ML), and convert to DataFrame
        if method == 'ml':
            loadings = self._normalize_ml(res.x, corr, n_factors)
        else:
            loadings = self._normalize_wls(res.x, corr, n_factors)

        loadings = pd.DataFrame(loadings,
                                index=data.columns.values,
                                columns=columns)
        return loadings

    def analyze(self,
                data,
                n_factors=3,
                rotation='promax',
                method='minres',
                use_smc=True,
                bounds=(0.005, 1),
                normalize=True,
                impute='median'):
        """
        Perform factor analysis.

        Parameters
        ----------
        data : pd.DataFrame
            The data to analyze.
        n_factors : int
            The number of factors to select.
            Defaults to 3.
        rotation : {'varimax', 'promax'} or None
            The type of rotation to perform after
            fitting the factor analysis model.
            If set to None, no rotation will be performed,
            nor will any associated Kaiser normalization.
            Defaults to 'promax'.
        method : {'minres', 'ml'}
            The fitting method to use, either MINRES or
            Maximum Likelihood.
            Defaults to 'minres'.
        use_smc : bool
            Whether to use squared multiple correlation
            as starting guesses for factor analysis.
            Defaults to True.
        bounds : tuple
            The lower and upper bounds on the variables
            for "L-BFGS-B" optimization.
            Defaults to (0.005, 1).
        normalize : bool
            Whether to perform Kaiser normalization
            and de-normalization prior to and following
            rotation.
            Defaults to True.
        impute : {'drop', 'mean', 'median'}
            If missing values are present in the data, either use
            list-wise deletion ('drop') or impute the column median
            ('median') or column mean ('mean').
            Defaults to 'median'.

        Raises
        ------
        ValueError
            If rotation not in {'varimax', 'promax', None}.
        ValueError
            If missing values present and `missing_values` is
            not set to either 'drop' or 'impute'.
        ValueError
            If a ValueError is raised in attempting to scale
            the data, possibly due to infinite values.

        Notes
        -----
        varimax is an orthogonal rotation, while promax
        is an oblique rotation. For more details on promax
        rotations, see here:

        https://www.rdocumentation.org/packages/psych/
        versions/1.7.8/topics/Promax
        """

        if rotation not in {'varimax', 'promax', None}:
            raise ValueError("The value for `rotation` must be in the "
                             "set: {'varimax', 'promax', None}.")

        df = data.copy()

        # remove non-numeric columns
        df = self.remove_non_numeric(df)

        if df.isnull().any().any():

            # impute median, if `impute` is set to 'median'
            if impute == 'median':
                df = df.apply(lambda x: x.fillna(x.median()), axis=0)

            # impute mean, if `impute` is set to 'mean'
            elif impute == 'mean':
                df = df.apply(lambda x: x.fillna(x.mean()), axis=0)

            # drop missing if `impute` is set to 'drop'
            elif impute == 'drop':
                df = df.dropna()

            else:
                raise ValueError("You have missing values in your data, but "
                                 "`impute` was not set to either 'drop', "
                                 "'mean', or 'median'.")

        # try scaling the data
        try:
            X = scale(df)
        except ValueError as error:
            raise ValueError('Could not scale the data. This may be due to '
                             'infinite values in your data.')

        X = pd.DataFrame(X, columns=df.columns)

        # fit factor analysis model
        loadings = self.fit_factor_analysis(X,
                                            n_factors,
                                            use_smc,
                                            bounds,
                                            method)

        # default rotation matrix to None
        rotation_mtx = None

        # whether to rotate the loadings matrix
        if rotation is not None:

            if rotation == 'varimax':
                loadings, rotation_mtx = self.varimax(loadings, normalize=normalize)
            elif rotation == 'promax':
                loadings, rotation_mtx = self.promax(loadings, normalize=normalize)

        self.corr = df.corr()
        self.loadings = loadings
        self.rotation_matrix = rotation_mtx

    def varimax(self, data, normalize=True, max_iter=500, tolerance=1e-5):
        """
        Varimax (orthogonal) rotation.

        Parameters
        ----------
        data : pd.DataFrame
            The loadings matrix to rotate.
        normalize : bool
            Whether to perform Kaiser normalization
            and de-normalization prior to and following
            rotation.
            Defaults to True.
        max_iter : int
            Maximum number of iterations.
            Defaults to 500.
        tolerance : float
            The tolerance for convergence.
            Defaults to 1e-5.

        Return
        ------
        loadings : pd.DataFrame
            The loadings matrix
            (n_cols X n_factors)
        rotation_mtx : np.array
            The rotation matrix
            (n_factors X n_factors)
        """
        df = data.copy()

        column_names = df.index.values
        index_names = df.columns.values

        n_rows, n_cols = df.shape

        if n_cols < 2:
            return df

        X = df.as_matrix()

        # normalize the loadings matrix
        # using sqrt of the sum of squares (Kaiser)
        if normalize:
            normalized_mtx = df.apply(lambda x: np.sqrt(sum(x**2)),
                                      axis=1).as_matrix()

            X = (X.T / normalized_mtx).T

        # initialize the rotation matrix
        # to N x N identity matrix
        rotation_mtx = np.eye(n_cols)

        d = 0
        for _ in range(max_iter):

            old_d = d

            # take inner product of loading matrix
            # and rotation matrix
            basis = np.dot(X, rotation_mtx)

            # transform data for singular value decomposition
            transformed = np.dot(X.T, basis**3 - (1.0 / n_rows) *
                                 np.dot(basis, np.diag(np.diag(np.dot(basis.T, basis)))))

            # perform SVD on
            # the transformed matrix
            U, S, V = np.linalg.svd(transformed)

            # take inner product of U and V, and sum of S
            rotation_mtx = np.dot(U, V)
            d = np.sum(S)

            # check convergence
            if old_d != 0 and d / old_d < 1 + tolerance:
                break

        # take inner product of loading matrix
        # and rotation matrix
        X = np.dot(X, rotation_mtx)

        # de-normalize the data
        if normalize:
            X = X.T * normalized_mtx

        else:
            X = X.T

        # convert loadings matrix to dataframe
        loadings = pd.DataFrame(X,
                                columns=column_names,
                                index=index_names).T

        return loadings, rotation_mtx

    def promax(self, data, normalize=False, power=4):
        """
        Promax (oblique) rotation.

        Parameters
        ----------
        data : pd.DataFrame
            The loadings matrix to rotate.
        normalize : bool
            Whether to perform Kaiser normalization
            and de-normalization prior to and following
            rotation.
            Defaults to False.
        power : int
            The power to which to raise the varimax loadings
            (minus 1). Numbers should generally range form 2 to 4.
            Defaults to 4.

        Return
        ------
        loadings : pd.DataFrame
            The loadings matrix
            (n_cols X n_factors)
        rotation_mtx : np.array
            The rotation matrix
            (n_factors X n_factors)
        """
        df = data.copy()

        column_names = df.index.values
        index_names = df.columns.values

        n_rows, n_cols = df.shape

        if n_cols < 2:
            return df

        if normalize:

            # pre-normalization is done in R's
            # `kaiser()` function when rotate='Promax'.
            array = df.as_matrix()
            h2 = sp.diag(np.dot(array, array.T))
            h2 = np.reshape(h2, (h2.shape[0], 1))
            weights = array / sp.sqrt(h2)

            # convert back to DataFrame for `varimax`
            weights = pd.DataFrame(weights,
                                   columns=index_names,
                                   index=column_names)
        else:
            weights = df.copy()

        # first get varimax rotation
        X, rotation_mtx = self.varimax(weights, normalize=normalize)
        Y = X * np.abs(X)**(power - 1)

        # fit linear regression model
        linear_regression = LinearRegression(fit_intercept=False)
        linear_regression.fit(X, Y)

        # get coefficients, and transpose them
        coef = linear_regression.coef_
        coef = coef.T

        # calculate diagonal of inverse square
        try:
            diag_inv = sp.diag(sp.linalg.inv(sp.dot(coef.T, coef)))
        except np.linalg.LinAlgError:
            diag_inv = sp.diag(sp.linalg.pinv(sp.dot(coef.T, coef)))

        # transform and calculate inner products
        coef = sp.dot(coef, sp.diag(sp.sqrt(diag_inv)))
        z = sp.dot(X, coef)

        if normalize:

            # post-normalization is done in R's
            # `kaiser()` function when rotate='Promax'
            z = z * sp.sqrt(h2)

        rotation_mtx = sp.dot(rotation_mtx, coef)

        # convert loadings matrix to DataFrame
        loadings = pd.DataFrame(z,
                                columns=index_names,
                                index=column_names)

        return loadings, rotation_mtx

    def get_eigenvalues(self):
        """
        Calculate the eigenvalues, given the
        factor correlation matrix.

        Return
        ------
        eigenvalues : pd.DataFrame
            A dataframe with eigenvalues information.
        """
        if (self.corr is not None and self.loadings is not None):

            corr = self.corr.as_matrix()

            e_values, _ = sp.linalg.eig(corr)
            e_values = np.real(e_values)
            e_values = pd.DataFrame(sorted(e_values, reverse=True),
                                    columns=['Original_Eigenvalues'])

            communalities = self.get_communalities()
            np.fill_diagonal(corr, communalities)

            values, _ = sp.linalg.eig(corr)
            values = np.real(values)
            values = pd.DataFrame(sorted(values, reverse=True),
                                  columns=['Common_Factor_Eigenvalues'])

            return e_values, values

    def get_communalities(self):
        """
        Calculate the communalities, given the
        factor loading matrix.

        Return
        ------
        communalities : pd.DataFrame
            A dataframe with communalities information.
        """
        if self.loadings is not None:

            communalities = (self.loadings ** 2).sum(axis=1)
            communalities = pd.DataFrame(communalities,
                                         columns=['Communalities'])

            return communalities

    def get_uniqueness(self):
        """
        Calculate the communalities, given the
        factor loading matrix.

        Return
        ------
        communalities : pd.DataFrame
            A dataframe with communalities information.
        """
        if self.loadings is not None:

            communalities = self.get_communalities()
            uniqueness = (1 - communalities)
            uniqueness.columns = ['Uniqueness']
            return uniqueness

    def get_factor_variance(self):
        """
        Calculate the factor variance information,
        including variance, proportional variance
        and cumulative variance.

        Return
        ------
        variance_info : pd.DataFrame
            A dataframe with variance information.
        """
        if self.loadings is not None:

            loadings = self.loadings

            n_rows = loadings.shape[0]

            # calculate variance
            loadings = loadings ** 2
            variance = loadings.sum(axis=0)

            # calculate proportional variance
            proportional_variance = variance / n_rows

            # calculate cumulative variance
            cumulative_variance = proportional_variance.cumsum(axis=0)

            # package variance info
            variance_info = pd.DataFrame([variance,
                                          proportional_variance,
                                          cumulative_variance],
                                         index=['SS Loadings',
                                                'Proportion Var',
                                                'Cumulative Var'])

            return variance_info


def main():
    """ Run the script.
    """

    # set up an argument parser
    parser = argparse.ArgumentParser(prog='factor_analyzer.py')
    parser.add_argument(dest='feature_file',
                        help="Input file containing the pre-processed features "
                             "for the training data")
    parser.add_argument(dest='output_dir', help="Output directory to save "
                                                "the output files", )
    parser.add_argument('-f', '--factors', dest="num_factors", type=int,
                        default=3, help="Number of factors to use (Default 3)",
                        required=False)

    parser.add_argument('-r', '--rotation', dest="rotation", type=str,
                        default='none', help="The rotation to perform (Default 'none')",
                        required=False)

    parser.add_argument('-m', '--method', dest="method", type=str,
                        default='minres', help="The method to use (Default 'minres')",
                        required=False)

    # parse given command line arguments
    args = parser.parse_args()

    method = args.method
    factors = args.num_factors
    rotation = None if args.rotation == 'none' else args.rotation

    file_path = args.feature_file

    data = read_file(file_path)

    # get the logger
    logger = logging.getLogger(__name__)
    logging.setLevel(logging.INFO)

    # log some useful messages so that the user knows
    logger.info("Starting exploratory factor analysis on: {}.".format(file_path))

    # run the analysis
    analyzer = FactorAnalyzer()
    analyzer.analyze(data, factors, rotation, method)

    # create paths to loadings loadings, eigenvalues, communalities, variance
    path_loadings = os.path.join(args.output_dir, 'loadings.csv')
    path_eigen = os.path.join(args.output_dir, 'eigenvalues.csv')
    path_communalities = os.path.join(args.output_dir, 'communalities.csv')
    path_variance = os.path.join(args.output_dir, 'variance.csv')

    # retrieve loadings, eigenvalues, communalities, variance
    loadings = analyzer.loadings
    eigen, _ = analyzer.get_eigenvalues()
    communalities = analyzer.get_communalities()
    variance = analyzer.get_factor_variance()

    # save the files
    logger.info("Saving files...")
    loadings.to_csv(path_loadings)
    eigen.to_csv(path_eigen)
    communalities.to_csv(path_communalities)
    variance.to_csv(path_variance)


if __name__ == '__main__':

    main()