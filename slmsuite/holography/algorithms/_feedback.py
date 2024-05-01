from slmsuite.holography.algorithms._header import *
from slmsuite.holography.algorithms._hologram import Hologram


class FeedbackHologram(Hologram):
    """
    Experimental holography aided by camera feedback.
    Contains mechanisms for hologram positioning and camera feedback aided by a
    :class:`~slmsuite.hardware.cameraslms.FourierSLM`.

    Attributes
    ----------
    cameraslm : slmsuite.hardware.cameraslms.FourierSLM OR None
        A hologram with experimental feedback needs access to an SLM and camera.
        If None, no feedback is applied (mostly defaults to :class:`Hologram`).
    cam_shape : (int, int)
        Shape of the camera in the meaning of :meth:`numpy.shape()`.
    cam_points : numpy.ndarray
        Array containing points corresponding to the corners of the camera in the SLM's k-space.
        First point is repeated at the end for easy plotting.
    target_ij :  array_like OR None
        Amplitude target in the ``"ij"`` (camera) basis. Of same ``shape`` as the camera in
        :attr:`cameraslm`.  Counterpart to :attr:`target` which is in the ``"knm"``
        (computational k-space) basis.
    img_ij, img_knm
        Cached **amplitude** feedback image in the
        ``"ij"`` (raw camera) basis or
        ``"knm"`` (transformed to computational k-space) basis.
        Measured with :meth:`.measure()`.
    """

    def __init__(self, shape, target_ij=None, cameraslm=None, **kwargs):
        """
        Initializes a hologram with camera feedback.

        Parameters
        ----------
        shape : (int, int)
            Computational shape of the SLM in :mod:`numpy` `(h, w)` form. See :meth:`.Hologram.__init__()`.
        target_ij : array_like OR None
            See :attr:`target_ij`. Should only be ``None`` if the :attr:`target`
            will be generated by other means (see :class:`SpotHologram`), so the
            user should generally provide an array.
        cameraslm : slmsuite.hardware.cameraslms.FourierSLM OR slmsuite.hardware.slms.SLM OR None
            Provides access to experimental feedback.
            If an :class:`slmsuite.hardware.slms.SLM` is passed, this is set to `None`,
            but the information contained in the SLM is passed to the superclass :class:`.Hologram`.
            See :attr:`cameraslm`.
        **kwargs
            See :meth:`Hologram.__init__`.
        """
        # Use the Hologram constructor to initialize self.target with proper shape,
        # pass other arguments (esp. slm_shape).
        self.cameraslm = cameraslm
        if self.cameraslm is not None:
            # Determine camera size in SLM-space.
            try:
                amp = self.cameraslm.slm._get_source_amplitude()
                slm_shape = self.cameraslm.slm.shape
            except:
                # See if an SLM was passed.
                try:
                    amp = self.cameraslm._get_source_amplitude()
                    slm_shape = self.cameraslm.shape

                    # We don't have access to all the calibration stuff, so don't
                    # confuse the rest of the init/etc.
                    self.cameraslm = None
                except:
                    raise ValueError("Expected a CameraSLM or SLM to be passed to cameraslm.")

        else:
            amp = kwargs.pop("amp", None)
            slm_shape = None

        if not "slm_shape" in kwargs:
            kwargs["slm_shape"] = slm_shape

        super().__init__(target=shape, amp=amp, **kwargs)

        self.img_ij = None
        self.img_knm = None
        if target_ij is None:
            self.target_ij = None
        else:
            self.target_ij = target_ij.astype(self.dtype)

        if self.cameraslm is not None and self.cameraslm.fourier_calibration is not None:
            # Generate a list of the corners of the camera, for plotting.
            cam_shape = self.cameraslm.cam.shape

            ll = [0, 0]
            lr = [0, cam_shape[0] - 1]
            ur = [cam_shape[1] - 1, cam_shape[0] - 1]
            ul = [cam_shape[1] - 1, 0]

            points_ij = toolbox.format_2vectors(np.vstack((ll, lr, ur, ul, ll)).T)
            points_kxy = self.cameraslm.ijcam_to_kxyslm(points_ij)
            self.cam_points = toolbox.convert_vector(
                points_kxy, "kxy", "knm", slm=self.cameraslm.slm, shape=self.shape
            )
            self.cam_shape = cam_shape

            # Transform the target, if it is provided.
            if target_ij is not None:
                self.update_target(target_ij, reset_weights=True)

        else:
            self.cam_points = None
            self.cam_shape = None

    # Image transformation helper function.
    def ijcam_to_knmslm(self, img, out=None, blur_ij=None, order=3):
        """
        Convert an image in the camera domain to computational SLM k-space using, in part, the
        affine transformation stored in a cameraslm's Fourier calibration.

        Note
        ~~~~
        This includes two transformations:

        - The affine transformation ``"ij"`` -> ``"kxy"`` (camera pixels to normalized k-space).
        - The scaling ``"kxy"`` -> ``"knm"`` (normalized k-space to computational k-space pixels).

        Parameters
        ----------
        img : numpy.ndarray OR cupy.ndarray
            Image to transform. This should be the same shape as images returned by the camera.
        out : numpy.ndarray OR cupy.ndarray OR None
            If ``out`` is not ``None``, this array will be used to write the memory in-place.
        blur_ij : int OR None
            Applies a ``blur_ij`` pixel-width Gaussian blur to ``img``.
            If ``None``, defaults to the ``"blur_ij"`` flag if present; otherwise zero.
        order : int
            Order of interpolation used for transformation. Defaults to 3 (cubic).

        Returns
        -------
        numpy.ndarray OR cupy.ndarray
            Image transformed into ``"knm"`` space.
        """
        assert self.cameraslm is not None
        assert self.cameraslm.fourier_calibration is not None

        # First transformation.
        conversion = toolbox.convert_vector(
            (1, 1), "knm", "kxy", slm=self.cameraslm.slm, shape=self.shape
        ) - toolbox.convert_vector(
            (0, 0), "knm", "kxy", slm=self.cameraslm.slm, shape=self.shape
        )
        M1 = np.diag(np.squeeze(conversion))
        b1 = np.matmul(M1, -toolbox.format_2vectors(np.flip(np.squeeze(self.shape)) / 2))

        # Second transformation.
        M2 = self.cameraslm.fourier_calibration["M"]
        b2 = self.cameraslm.fourier_calibration["b"] - np.matmul(
            M2, self.cameraslm.fourier_calibration["a"]
        )

        # Composite transformation (along with xy -> yx).
        M = cp.array(np.flip(np.flip(np.matmul(M2, M1), axis=0), axis=1))
        b = cp.array(np.flip(np.matmul(M2, b1) + b2))

        # See if the user wants to blur.
        if blur_ij is None:
            if "blur_ij" in self.flags:
                blur_ij = self.flags["blur_ij"]
            else:
                blur_ij = 0

        # FUTURE: use cp_gaussian_filter (faster?); was having trouble with cp_gaussian_filter.
        if blur_ij > 0:
            img = sp_gaussian_filter(img, (blur_ij, blur_ij), output=img, truncate=2)

        cp_img = cp.array(img, dtype=self.dtype)
        cp.abs(cp_img, out=cp_img)

        # Perform affine.
        target = cp_affine_transform(
            input=cp_img,
            matrix=M,
            offset=b,
            output_shape=self.shape,
            order=order,
            output=out,
            mode="constant",
            cval=0,
        )

        # Filter the image. FUTURE: fix.
        # target = cp_gaussian_filter1d(target, blur, axis=0, output=target, truncate=2)
        # target = cp_gaussian_filter1d(target, blur, axis=1, output=target, truncate=2)

        target = cp.abs(target, out=target)
        norm = Hologram._norm(target)
        target *= 1 / norm

        if norm == 0:
            raise ValueError(
                "No power in hologram. Maybe target_ij is out of range of knm space? "
                "Check transformations."
            )

        return target

    # Measurement.
    def measure(self, basis="ij"):
        """
        Method to request a measurement to occur. If :attr:`img_ij` is ``None``,
        then a new image will be grabbed from the camera (this is done automatically in
        algorithms).

        Parameters
        ----------
        basis : str
            The cached image to be sure to fill with new data.
            Can be ``"ij"`` or ``"knm"``.

            - If ``"knm"``, then :attr:`img_ij` and :attr:`img_knm` are filled.
            - If ``"ij"``, then :attr:`img_ij` is filled, and :attr:`img_knm` is ignored.

            This is useful to avoid (expensive) transformation from the ``"ij"`` to the
            ``"knm"`` basis if :attr:`img_knm` is not needed.
        """
        if self.img_ij is None:
            self.cameraslm.slm.write(self.extract_phase(), settle=True)
            self.cameraslm.cam.flush()
            self.img_ij = np.array(self.cameraslm.cam.get_image(), copy=False, dtype=self.dtype)

            if basis == "knm":  # Compute the knm basis image.
                self.img_knm = self.ijcam_to_knmslm(self.img_ij, out=self.img_knm)
                cp.sqrt(self.img_knm, out=self.img_knm)
            else:  # The old image is outdated, erase it. FUTURE: memory concerns?
                self.img_knm = None

            self.img_ij = np.sqrt(self.img_ij)  # Don't load to the GPU if not necessary.
        elif basis == "knm":
            if self.img_knm is None:
                self.img_knm = self.ijcam_to_knmslm(np.square(self.img_ij), out=self.img_knm)
                cp.sqrt(self.img_knm, out=self.img_knm)

    # Target update.
    def update_target(self, new_target, reset_weights=False, plot=False):
        # Transformation order of zero to prevent nan-blurring in MRAF cases.
        self.ijcam_to_knmslm(new_target, out=self.target, order=0)

        if reset_weights:
            self.reset_weights()

        if plot:
            self.plot_farfield(self.target)

    def refine_offset(self, img, basis="kxy"):
        """
        **(NotImplemented)**
        Hones the position of the produced image to the desired target image to compensate for
        Fourier calibration imperfections. Works either by moving the desired camera
        target to align where the image ended up (``basis="ij"``) or by moving
        the :math:`k`-space image to target the desired camera target
        (``basis="knm"``/``basis="kxy"``). This should be run at the user's request
        inbetween :meth:`optimize` iterations.

        Parameters
        ----------
        img : numpy.ndarray
            Image measured by the camera.
        basis : str
            The correction can be in any of the following bases:
            - ``"ij"`` changes the pixel that the spot is expected at,
            - ``"kxy"`` or ``"knm"`` changes the k-vector which the SLM targets.
            Defaults to ``"kxy"`` if ``None``.

        Returns
        -------
        numpy.ndarray
            Euclidean pixel error in the ``"ij"`` basis for each spot.
        """

        raise NotImplementedError()

    # Weighting and stats.
    def _update_weights(self):
        """
        Change :attr:`weights` to optimize towards the :attr:`target` using feedback from
        :attr:`amp_ff`, the computed farfield amplitude.
        """
        feedback = self.flags["feedback"]

        if feedback == "computational":
            self._update_weights_generic(self.weights, self.amp_ff, self.target)
        elif feedback == "experimental":
            self.measure("knm")  # Make sure data is there.
            self._update_weights_generic(self.weights, self.img_knm, self.target)

    def _calculate_stats_experimental(self, stats, stat_groups=[]):
        """
        Wrapped by :meth:`FeedbackHologram.update_stats()`.
        """
        if "experimental_knm" in stat_groups:
            self.measure("knm")  # Make sure data is there.

            stats["experimental_knm"] = self._calculate_stats(
                self.img_knm,
                self.target,
                efficiency_compensation=True,
                raw="raw_stats" in self.flags and self.flags["raw_stats"],
            )
        if "experimental_ij" in stat_groups or "experimental" in stat_groups:
            self.measure("ij")  # Make sure data is there.

            stats["experimental_ij"] = self._calculate_stats(
                self.img_ij.astype(self.dtype),
                self.target_ij,
                xp=np,
                efficiency_compensation=True,
                raw="raw_stats" in self.flags and self.flags["raw_stats"],
            )

    def update_stats(self, stat_groups=[]):
        """
        Calculate statistics corresponding to the desired ``stat_groups``.

        Parameters
        ----------
        stat_groups : list of str
            Which groups or types of statistics to analyze.
        """
        stats = {}

        self._calculate_stats_computational(stats, stat_groups)
        self._calculate_stats_experimental(stats, stat_groups)

        self._update_stats_dictionary(stats)
