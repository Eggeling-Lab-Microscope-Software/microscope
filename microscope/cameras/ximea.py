#!/usr/bin/env python3

## Copyright (C) 2020 David Miguel Susano Pinto <carandraug@gmail.com>
## Copyright (C) 2020 Ian Dobbie <ian.dobbie@bioch.ox.ac.uk>
## Copyright (C) 2020 Mick Phillips <mick.phillips@gmail.com>
##
## This file is part of Microscope.
##
## Microscope is free software: you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation, either version 3 of the License, or
## (at your option) any later version.
##
## Microscope is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with Microscope.  If not, see <http://www.gnu.org/licenses/>.

"""Ximea cameras.

Changing settings flushes the buffer
------------------------------------

It is not possible to set some parameters during image acquisition.
In such cases, acquisition is stopped (camera is disabled) and the
restarted (camera is enabled).  However, stopping acquisition discards
any image in the camera memory that have not yet been read.

Modifying the following settings require acquisition to be stopped:

- ROIs
- binning
- trigger type (trigger source)

For more details, see the [XiAPI manual](https://www.ximea.com/support/wiki/apis/XiAPI_Manual#Flushing-the-queue).

Hardware trigger
----------------

Ximea cameras in the MQ family accept software triggers even if set
for hardware triggers (see `vendor issues
#3`<https://github.com/python-microscope/vendor-issues/issues/3>).
However, `XimeaCamera.trigger()` checks the trigger type and will
raise an exception unless the camera is set for software triggers.

Requirements
------------

Support for Ximea cameras requires Ximea's API Python (xiApiPython).
This is only available via Ximea's website and is not available on
PyPI.  See Ximea's website for `install instructions
<https://www.ximea.com/support/wiki/apis/Python>`__.

"""

import contextlib
import enum
import logging
import typing

import numpy as np
from ximea import xiapi

import microscope
import microscope.abc


_logger = logging.getLogger(__name__)


# The ximea package does not provide an enum for the error codes.
# There is ximea.xidefs.ERROR_CODES which maps the error code to an
# error message but what we need is a symbol that maps to the error
# code so we can use while handling exceptions.
_XI_TIMEOUT = 10
_XI_INVALID_ARGUMENTS = 11
_XI_NOT_SUPPORTED = 12
_XI_NOT_IMPLEMENTED = 26
_XI_ACQUISITION_STOPED = 45
_XI_UNKNOWN_PARAM = 100


# During acquisition, we rely on catching timeout errors which then
# get discarded.  However, with debug level set to warning (XiApi
# default log level), we get XiApi messages on stderr for each timeout
# making logging impossible.  So change this to error.
#
# Debug level is a xiapi global setting but we need a Camera instance.
xiapi.Camera().set_debug_level("XI_DL_ERROR")


@contextlib.contextmanager
def _disabled_camera(camera):
    """Context manager to temporarily disable camera."""
    if camera.enabled:
        try:
            camera.disable()
            yield camera
        finally:
            camera.enable()
    else:
        yield camera


@contextlib.contextmanager
def _enabled_camera(camera):
    """Context manager to temporarily enable camera."""
    if not camera.enabled:
        try:
            camera.enable()
            yield camera
        finally:
            camera.disable()
    else:
        yield camera


TRIGGER_TABLE: dict[tuple[microscope.TriggerType, microscope.TriggerMode], tuple[str, str]] = {
    (microscope.TriggerType.SOFTWARE, microscope.TriggerMode.STROBE): ("XI_TRG_OFF", "XI_TRG_SEL_FRAME_START"),
    (microscope.TriggerType.SOFTWARE, microscope.TriggerMode.ONCE): ("XI_TRG_SOFTWARE", "XI_TRG_SEL_FRAME_START"),
    (microscope.TriggerType.RISING_EDGE, microscope.TriggerMode.ONCE): ("XI_TRG_EDGE_RISING", "XI_TRG_SEL_FRAME_START"),
    (microscope.TriggerType.RISING_EDGE, microscope.TriggerMode.ONCE): ("XI_TRG_EDGE_RISING", "XI_TRG_SEL_FRAME_START")
}


class XimeaCamera(microscope.abc.Camera):
    """Ximea cameras

    Args:
        serial_number: the serial number of the camera to connect to.
            It can be set to `None` if there is only camera on the
            system.

    """

    def __init__(
        self, serial_number: typing.Optional[str] = None, **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._acquiring = False
        self._handle = xiapi.Camera()
        self._img = xiapi.Image()
        self._serial_number = serial_number
        self._sensor_shape = (0, 0)
        self._roi = microscope.ROI(None, None, None, None)
        self._binning = microscope.Binning(1, 1)
        self._trigger_map = TRIGGER_TABLE[(
            microscope.TriggerType.SOFTWARE, microscope.TriggerMode.ONCE)]

        self.initialize()

    def _fetch_data(self) -> typing.Optional[np.ndarray]:
        if not self._acquiring:
            return None

        try:
            self._handle.get_image(self._img, timeout=1)
        except xiapi.Xi_error as err:
            # err.status may not exist so use getattr (see
            # https://github.com/python-microscope/vendor-issues/issues/2)
            if getattr(err, "status", None) == _XI_TIMEOUT:
                return None
            elif (
                getattr(err, "status", None) == _XI_ACQUISITION_STOPED
                and not self._acquiring
            ):
                # We can end up here during disable if self._acquiring
                # was True but is now False.
                return None
            else:
                raise

        data: np.ndarray = self._img.get_image_data_numpy()
        _logger.info(
            "Fetched imaged with dims %s and size %s.", data.shape, data.size
        )
        return data

    def abort(self):
        _logger.info("Disabling acquisition.")
        if self._acquiring:
            # We set acquiring before calling stop_acquisition because
            # the fetch loop is still running and will raise errors 45
            # otherwise.
            self._acquiring = False
            try:
                self._handle.stop_acquisition()
            except Exception:
                self._acquiring = True
                raise

    def initialize(self) -> None:
        """Initialise the camera.

        Open the connection, connect properties and populate settings dict.
        """
        n_cameras = self._handle.get_number_devices()

        if self._serial_number is None:
            if n_cameras > 1:
                raise TypeError(
                    "more than one Ximea camera found but the"
                    " serial_number argument was not specified"
                )
            _logger.info(
                "serial_number is not specified but there is only one"
                " camera on the system"
            )
            self._handle.open_device()
        else:
            _logger.info(
                "opening camera with serial number '%s'", self._serial_number
            )
            self._handle.open_device_by_SN(self._serial_number)

        self._sensor_shape = (
            self._handle.get_width_maximum()
            + self._handle.get_offsetX_maximum(),
            self._handle.get_height_maximum()
            + self._handle.get_offsetY_maximum(),
        )
        self._roi = microscope.ROI(
            left=0,
            top=0,
            width=self._sensor_shape[0],
            height=self._sensor_shape[1],
        )
        self.set_roi(self._roi)

        self.set_trigger(
            microscope.TriggerType.SOFTWARE, microscope.TriggerMode.ONCE
        )

        # Add settings for the different temperature sensors.
        for temp_param_name in [
            "chip_temp",
            "hous_temp",
            "hous_back_side_temp",
            "sensor_board_temp",
        ]:
            get_temp_method = getattr(self._handle, "get_" + temp_param_name)
            # Not all cameras have temperature sensors in all
            # locations.  We can't query if the sensor is there, we
            # can only try to read the temperature and skip that
            # temperature sensor if we get an exception.
            try:
                get_temp_method()
            except xiapi.Xi_error as err:
                if err.status != _XI_NOT_SUPPORTED and err.status != _XI_NOT_IMPLEMENTED:
                    raise
            else:
                self.add_setting(
                    temp_param_name,
                    "float",
                    get_temp_method,
                    None,
                    values=tuple(),
                )

    def _do_disable(self):
        self.abort()

    def _do_enable(self):
        _logger.info("Preparing for acquisition.")
        if self._acquiring:
            self.abort()
        # actually start camera
        self._handle.start_acquisition()
        self._acquiring = True
        _logger.info("Acquisition enabled.")
        return True

    def set_exposure_time(self, value: float) -> None:
        # exposure times are set in us.
        try:
            self._handle.set_exposure_direct(int(value * 1000000))
        except Exception as err:
            _logger.debug("set_exposure_time exception: %s", err)

    def get_exposure_time(self) -> float:
        # exposure times are in us, so multiple by 1E-6 to get seconds.
        return self._handle.get_exposure() * 1.0e-6

    def get_cycle_time(self):
        return 1.0 / self._handle.get_framerate()

    def _get_sensor_shape(self) -> typing.Tuple[int, int]:
        return self._sensor_shape

    def soft_trigger(self) -> None:
        self.trigger()

    def _do_trigger(self) -> None:
        # Value for set_trigger_software() has no meaning.  See
        # https://github.com/python-microscope/vendor-issues/issues/3
        if self._trigger_map == TRIGGER_TABLE[(microscope.TriggerType.SOFTWARE, microscope.TriggerMode.ONCE)]:
            self._handle.set_trigger_software(1)

    def _get_binning(self) -> microscope.Binning:
        return self._binning

    def _set_binning(self, binning: microscope.Binning) -> bool:
        if binning == self._binning:
            return True
        # We don't have a ximea camera that supports binning so we
        # can't write support for this (a camera without this feature
        # will raise error 100).  When writing this, careful and check
        # what XiAPI does when mixing ROI and binning.
        raise NotImplementedError()

    def _get_roi(self) -> microscope.ROI:
        assert self._roi == microscope.ROI(
            self._handle.get_offsetX(),
            self._handle.get_offsetY(),
            self._handle.get_width(),
            self._handle.get_height(),
        ), "ROI attribute is out of sync with internal camera setting"
        return self._roi

    def _set_roi(self, roi: microscope.ROI) -> bool:
        if (
            roi.width + roi.left > self._sensor_shape[0]
            or roi.height + roi.top > self._sensor_shape[1]
        ):
            raise ValueError(
                "ROI %s does not fit in sensor shape %s"
                % (roi, self._sensor_shape)
            )
        try:
            # These methods will fail if the width/height plus their
            # corresponding offsets are higher than the sensor size.
            # So we start by setting the offset to zero.  Cases to
            # think off: 1) shrinking ROI size, 2) increasing ROI
            # size, 3) resetting ROI and so can't trust self._roi as
            # the current state (see this exception handling).
            with _disabled_camera(self):
                self._handle.set_offsetX(0)
                self._handle.set_offsetY(0)
                self._handle.set_width(roi.width)
                self._handle.set_height(roi.height)
                self._handle.set_offsetX(roi.left)
                self._handle.set_offsetY(roi.top)
        except xiapi.Xi_error as err:
            if err.status == _XI_INVALID_ARGUMENTS:
                with _disabled_camera(self):
                    # we don't need to set again
                    # the offsets to 0 as the exception
                    # is thrown only starting from
                    # set_width
                    height = roi.height
                    width = roi.width
                    left = roi.left
                    top = roi.top

                    # TODO: maybe these if conditions
                    # make the code less readable...
                    if height != self._roi.height:
                        h_incr = self._handle.get_height_increment()
                        height = (round(height / h_incr) *
                                  h_incr if (height % h_incr) != 0 else height)
                        self._handle.set_height(height)

                    if width != self._roi.width:
                        w_incr = self._handle.get_width_increment()
                        width = (round(width / w_incr) *
                                 w_incr if (width % w_incr) != 0 else width)
                        self._handle.set_width(width)

                    if left != self._roi.left:
                        l_incr = self._handle.get_offsetX_increment()
                        left = (round(left / l_incr)*l_incr if (left %
                                l_incr) != 0 else left)
                        self._handle.set_offsetX(left)

                    if top != self._roi.top:
                        t_incr = self._handle.get_offsetY_increment()
                        top = (round(top / t_incr)*t_incr if (top %
                               t_incr) != 0 else top)
                        self._handle.set_offsetY(top)
                    # we change input parameter roi so that
                    # the internal self._roi is updated as well
                    roi = microscope.ROI(left, top, width, height)
        except Exception:
            self._set_roi(self._roi)  # set it back to whatever was before
            raise
        self._roi = roi
        return True

    def _do_shutdown(self) -> None:
        if self._acquiring:
            self._handle.stop_acquisition()
        if self._handle.CAM_OPEN:
            # We check CAM_OPEN instead of try/catch an exception
            # because if the camera failed initialisation, XiApi fails
            # hard with error code -1009 (unknown) since the internal
            # device handler is NULL.
            self._handle.close_device()

    @property
    def trigger_mode(self) -> microscope.TriggerMode:
        return self._trigger_map[1]

    @property
    def trigger_type(self) -> microscope.TriggerType:
        return self._trigger_map[0]

    def set_trigger(
        self, ttype: microscope.TriggerType, tmode: microscope.TriggerMode
    ) -> None:
        try:
            new_map = TRIGGER_TABLE[(ttype, tmode)]
        except KeyError:
            raise microscope.UnsupportedFeatureError(
                f"TriggerType.{ttype.name} - TriggerMode.{tmode.name} not supported"
            )

        if new_map != self._trigger_map:
            with _disabled_camera(self):
                self._handle.set_trigger_source(new_map[0])
                self._handle.set_trigger_selector(new_map[1])
            self._trigger_map = new_map
