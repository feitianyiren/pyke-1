from . import DEFAULT_PRFDIR
from .utils import channel_to_module_output
from abc import ABC, abstractmethod
import os
import glob
import math
import scipy
import numpy as np
from astropy.io import fits as pyfits
from oktopus.models import get_initial_guesses
from oktopus.likelihood import PoissonLikelihood


__all__ = ['KeplerPRFPhotometry', 'KeplerPRF']


class PRFPhotometry(ABC):
    """An abstract base class for a general PRF/PSF photometry algorithm
    for target pixel files."""

    @abstractmethod
    def do_photometry(self, tpf, initial_guesses=None):
        """Perform photometry on a given target pixel file.

        Parameters
        ----------
        tpf : pyke.TargetPixelFile instance
            A target pixel file instance
        initial_guesses : None or array-like
            A vector of initial estimates for the PRF/PSF model
        """
        pass

    @abstractmethod
    def generate_residuals_movie(self):
        """Creates a movie showing the residuals (image - fitted stars)
        for every cadence.
        """
        pass


class PRFModel(ABC):
    """An abstract base class for a general PRF/PSF parametric model."""

    @abstractmethod
    def evaluate(self, params):
        """Builds the PRF model parametrized by params.

        Parameters
        ----------
        *params : list-like
            Parameter values used to build a PRF model.

        Returns
        -------
        prf_model : 2D array
            PRF/PSF model.
        """
        pass


class KeplerPRFPhotometry(PRFPhotometry):
    """
    This class performs PRF Photometry on a target pixel file from
    NASA's Kepler/K2 missions.

    Attributes
    ----------
    prf_model : instance of PRFModel
    """
    # Let's borrow as much as possible from photutils here. Ideally,
    # this could be a child class from BasicPSFPhotometry.

    def __init__(self, prf_model, loss_function=PoissonLikelihood):
        self.prf_model = prf_model
        self.loss_function = loss_function
        self.opt_params = []
        self.residuals = []
        self.uncertainties = []

    def do_photometry(self, tpf, initial_guesses=None):
        if initial_guesses is None:
            # this must be clever enough to find the number of stars
            # great way to go is to use photutils.detection.DAOStarFinder
            initial_guesses, _ = get_inital_guesses(tpf.flux)

        for t in range(len(tpf.time)):
            logL = self.loss_function(tpf.flux[t], self.prf_model)
            opt_result = logL.fit(*initial_guesses).x
            residuals_opt_result = tpf.flux - self.prf_model(*opt_result)
            initial_guesses = opt_result
            self.opt_params.append(opt_result)
            self.residuals.append(residuals_opt_result)
            self.uncertainties.append(logL.uncertainties())

        self.opt_params = self.opt_params.reshape((tpf.shape[0], len(initial_guesses)))
        self.uncertainties = self.uncertainties.reshape((tpf.shape[0], len(initial_guesses)))

    def generate_residuals_movie(self):
        pass


class KeplerPRF(object):
    """
    Kepler's Pixel Response Function

    This class provides the necessary interface to load Kepler PSF
    calibration files and to create a model that can be fit as a function
    of flux and centroid position.

    Attributes
    ----------
    prf_files_dir : str
        Relative or aboslute path to a directory containing the Pixel Response
        Function calibration files produced during Kepler data comissioning.
    channel : int
        KeplerTargetPixelFile.channel
    shape : (int, int)
        KeplerTargetPixelFile.shape
    column : int
        KeplerTargetPixelFile.column
    row : int
        KeplerTargetPixelFile.row
    """

    def __init__(self, channel, shape, column, row, prf_files_dir=DEFAULT_PRFDIR):
        self.prf_files_dir = prf_files_dir
        self.channel = channel
        self.shape = shape
        self.column = column
        self.row = row
        self.col_coord, self.row_coord, self.interpolate = self._prepare_prf()

    def prf_to_detector(self, flux, centroid_col, centroid_row, stretch_col=1,
                        stretch_row=1, rotation_radians=0):
        """
        Interpolates the PRF model onto detector coordinates.

        Parameters
        ----------
        flux : float or array-like
            Total integrated flux of the PRF
        centroid_col : float or array-like
            Column coordinate of the centroid
        centroid_row : float or array-like
            Row coordinate of the centroid

        Returns
        -------
        prf_model : 2D array
            Two dimensional array representing the PRF values parametrized
            by `params`.
        """
        cos_rot = math.cos(rotation_radians)
        sin_rot = math.sin(rotation_radians)
        delta_col = self.col_coord - centroid_col
        delta_row = self.row_coord - centroid_row
        rot_col = delta_col * cos_rot - delta_row[0] * sin_rot
        rot_row = delta_col[0] * sin_rot + delta_row * cos_rot
        self.prf_model = flux * self.interpolate(delta_row * stretch_row,
                                                 delta_col * stretch_col)
        return self.prf_model

    def evaluate(self, *args, **kwargs):
        return self.prf_to_detector(*args, **kwargs)

    def _read_prf_calibration_file(self, path, ext):
        prf_cal_file = pyfits.open(path)
        data = prf_cal_file[ext].data
        # looks like these data below are the same for all prf calibration files
        crval1p = prf_cal_file[ext].header['CRVAL1P']
        crval2p = prf_cal_file[ext].header['CRVAL2P']
        cdelt1p = prf_cal_file[ext].header['CDELT1P']
        cdelt2p = prf_cal_file[ext].header['CDELT2P']
        prf_cal_file.close()
        return data, crval1p, crval2p, cdelt1p, cdelt2p

    def _prepare_prf(self):
        n_hdu = 5
        min_prf_weight = 1e-6
        module, output = channel_to_module_output(self.channel)
        # determine suitable PRF calibration file
        if module < 10:
            prefix = 'kplr0'
        else:
            prefix = 'kplr'
        prf_file_path = os.path.join(self.prf_files_dir,
                                     prefix + str(module) + '.' + str(output) + '*_prf.fits')
        prffile = glob.glob(prf_file_path)[0]

        # read PRF images
        prfn = [0] * n_hdu
        crval1p = np.zeros(n_hdu, dtype='float32')
        crval2p = np.zeros(n_hdu, dtype='float32')
        cdelt1p = np.zeros(n_hdu, dtype='float32')
        cdelt2p = np.zeros(n_hdu, dtype='float32')
        for i in range(n_hdu):
            prfn[i], crval1p[i], crval2p[i], cdelt1p[i], cdelt2p[i] = self._read_prf_calibration_file(prffile, i+1)
        prfn = np.array(prfn)
        PRFcol = np.arange(0.5, np.shape(prfn[0])[1] + 0.5)
        PRFrow = np.arange(0.5, np.shape(prfn[0])[0] + 0.5)
        PRFcol = (PRFcol - np.size(PRFcol) / 2) * cdelt1p[0]
        PRFrow = (PRFrow - np.size(PRFrow) / 2) * cdelt2p[0]

        # interpolate the calibrated PRF shape to the target position
        rowdim, coldim = self.shape[0], self.shape[1]
        prf = np.zeros(np.shape(prfn[0]), dtype='float32')
        prfWeight = np.zeros(n_hdu, dtype='float32')
        ref_column = self.column + (coldim - 1.) / 2.
        ref_row = self.row + (rowdim - 1.) / 2.
        for i in range(n_hdu):
            prfWeight[i] = math.sqrt((ref_column - crval1p[i]) ** 2
                                     + (ref_row - crval2p[i]) ** 2)
            if prfWeight[i] < min_prf_weight:
                prfWeight[i] = min_prf_weight
            prf += prfn[i] / prfWeight[i]
        prf /= (np.nansum(prf) * cdelt1p[0] * cdelt2p[0])

        # location of the data image centered on the PRF image (in PRF pixel units)
        col_coord = np.arange(self.column + .5, self.column + coldim + .5)
        row_coord = np.arange(self.row + .5, self.row + rowdim + .5)
        interpolate = scipy.interpolate.RectBivariateSpline(PRFcol, PRFrow, prf)

        return col_coord, row_coord, interpolate
