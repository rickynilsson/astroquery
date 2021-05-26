# Licensed under a 3-clause BSD style license - see LICENSE.rst

# Basic imports
import copy
import io
import re
import warnings

# Import various astropy modules
import astropy.coordinates as coord
import astropy.units as u
import astropy.units.cds as cds
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import ascii
from astropy.io.votable import parse_single_table
from astropy.table import QTable
from astropy.utils import deprecated, deprecated_renamed_argument
from astropy.utils.exceptions import AstropyWarning

# Import astroquery utilities
from ..exceptions import (InputWarning, InvalidQueryError, NoResultsWarning,
                          RemoteServiceError)
from ..query import BaseQuery
from ..utils import async_to_sync, commons
from ..utils.class_or_instance import class_or_instance
from . import conf

# Import TAP client
# from astroquery.utils.tap.core import TapPlus # This package will be deprecated, use PyVO
import pyvo

# Objects exported when calling from astroquery.nasa_exoplanet_archive import *
__all__ = ["NasaExoplanetArchive", "NasaExoplanetArchiveClass"]

# Dictionary mapping unit strings to astropy units
UNIT_MAPPER = {
    "--": None,
    "BJD": None,  # TODO: optionally support mapping columns to Time objects
    "BKJD": None,  # TODO: optionally support mapping columns to Time objects
    "D_L": u.pc,
    "D_S": u.pc,
    "Earth flux": u.L_sun / (4 * np.pi * u.au**2),
    "Earth Flux": u.L_sun / (4 * np.pi * u.au**2),
    "Fearth": u.L_sun / (4 * np.pi * u.au**2),
    "M_E": u.M_earth,
    "Earth Mass": u.M_earth,
    "M_J": u.M_jupiter,
    "Jupiter Mass": u.M_jupiter,
    "R_Earth": u.R_earth,  # Add u.R_jupiter
    "Earth Radius": u.R_earth,
    "Jupiter Radius": u.R_jupiter,
    "R_Sun": u.R_sun,
    "Rstar": u.R_sun,
    "a_perp": u.au,
    "arc-sec/year": u.arcsec / u.yr,
    "cm/s**2": u.cm / u.s ** 2,
    "g/cm**3": u.g / u.cm ** 3,
    "days": u.day,
    "degrees": u.deg,
    "dexincgs": u.dex(u.cm / u.s ** 2),
    "hours": u.hr,
    "hrs": u.hr,
    "kelvin": u.K,
    "logLsun": u.dex(u.L_sun),
    "log(Solar)": u.dex(u.L_sun),
    "mags": u.mag,
    "microas": u.uas,
    "perc": u.percent,
    "pi_E": None,
    "pi_EE": None,
    "pi_EN": None,
    "pi_rel": None,
    "ppm": cds.ppm,
    "seconds": u.s,
    "Solar mass": u.M_sun,
    "solarradius": u.R_sun,
    "Solar Radius": u.R_sun,
    "log10(cm/s**2)": u.dex(u.cm / u.s ** 2),
    "dex": u.dex(None),
    "sexagesimal": None
}

# Converter for converting raw string values from the table into numeric data types. Dictionary items will be converters (converter function, converter type tuples) for consecutive columns.
CONVERTERS = dict(koi_quarters=[ascii.convert_numpy(str)])

# 'ps' and 'pscomppars' are the main tables of detected exoplanets. Calls to the old tables ('exoplanets', 'compositepars', 'exomultpars') will return errors and urge the user to call the 'ps' or 'pscomppars' tables
OBJECT_TABLES = {"ps": "pl_", "pscomppars": "pl_", "exoplanets": "pl_", "compositepars": "fpl_", "exomultpars": "mpl_"}
MAP_TABLEWARNINGS = {"exoplanets": "Planetary Systems (PS)", "compositepars": "Planetary System Composite Parameters table (PSCompPars)", "exomultpars": "Planetary Systems (PS)"}


class InvalidTableError(InvalidQueryError):
    """Exception thrown if the given table is not recognized by the Exoplanet Archive Servers"""

    pass


# Class decorator, async_to_sync, modifies NasaExoplanetArchiveClass to convert all query_x_async methods to query_x methods
@async_to_sync
class NasaExoplanetArchiveClass(BaseQuery):
    """
    The interface for querying the NASA Exoplanet Archive TAP and API services

    A full discussion of the available tables and query syntax is available on the documentation
    pages for `TAP <https://exoplanetarchive.ipac.caltech.edu/docs/TAP/usingTAP.html>`_ and `API <https://exoplanetarchive.ipac.caltech.edu/docs/program_interfaces.html>`_.
    """

    # When module us imported, __init__.py runs and loads a configuration object, setting the configuration parameters con.url, conf.timeout and conf.cache
    URL_API = conf.url_api
    URL_TAP = conf.url_tap
    TIMEOUT = conf.timeout
    CACHE = conf.cache

    # Ensures methods can be called either as class methods or instance methods. This is the basic query method.
    @class_or_instance
    def query_criteria_async(self, table, get_query_payload=False, cache=None, **criteria):
        """
        Search a table given a set of criteria or return the full table

        The syntax for these queries is described on the Exoplanet Archive TAP[1]_ API[2]_ documentation pages.
        In particular, the most commonly used criteria will be ``select`` and ``where``.

        Parameters
        ----------
        table : str
            The name of the table to query. A list of the tables on the Exoplanet Archive can be
            found on the documentation pages [1]_, [2]_.
        get_query_payload : bool, optional
            Just return the dict of HTTP request parameters. Defaults to ``False``.
        cache : bool, optional
            Should the request result be cached? This can be useful for large repeated queries,
            but since the data in the archive is updated regularly, this defaults to ``False``.
        **criteria
            The filtering criteria to apply. These are described in detail in the archive
            documentation [1]_, [2]_ but some examples include ``select="*"`` to return all columns of
            the queried table or ``where=pl_name='K2-18 b'`` to filter a specific column.

        Returns
        -------
        response : `requests.Response`
            The HTTP response returned from the service.

        References
        ----------

        .. [1] `NASA Exoplanet Archive TAP Documentation
           <https://exoplanetarchive.ipac.caltech.edu/docs/TAP/usingTAP.html>`_
        .. [2] `NASA Exoplanet Archive API Documentation
           <https://exoplanetarchive.ipac.caltech.edu/docs/program_interfaces.html>`_
        """
        # Make sure table is lower-case
        table = table.lower()

        # Warn if old table is requested
        if table in MAP_TABLEWARNINGS.keys():
            # warnings.warn("The '{0}' table is stale and will be depracated in the Archive 2.0 release. Use the 'ps' table. See https://exoplanetarchive.ipac.caltech.edu/docs/ps-pscp_release_notes.html".format(table), InputWarning, )
            raise InvalidTableError("The ``{0}`` table is no longer updated and has been replaced by the {1}, which is connected to the Exoplanet Archive TAP service. Although the argument keywords of the called method should still work on the new table, the allowed values could have changed since the database column names have changed; this document contains the current definitions and a mapping between the new and deprecated names: https://exoplanetarchive.ipac.caltech.edu/docs/API_PS_columns.html. You might also want to review the TAP User Guide for help on creating a new query for the most current data: https://exoplanetarchive.ipac.caltech.edu/docs/TAP/usingTAP.html.".format(table, MAP_TABLEWARNINGS[table]))

        # Deal with lists of columns instead of comma separated strings
        criteria = copy.copy(criteria)
        if "select" in criteria:
            select = criteria["select"]
            if not isinstance(select, str):
                select = ",".join(select)
            criteria["select"] = select

        # We prefer to work with IPAC format so that we get units, but everything it should work
        # with the other options too
        # Get the format, or set it to "ipac" if not given. Makes more sense to use CSV here.
        criteria["format"] = criteria.get("format", "ipac")
        # Less formats are allowed for TAP, so this needs to be updated. Default is VOTable (vot?, xml?), also csv and tsv are allowed
        if "json" in criteria["format"].lower():
            raise InvalidQueryError("The 'json' format is not supported")

        # Build the query (and return it if requested)
        request_payload = dict(table=table, **criteria)
        if get_query_payload:
            return request_payload

        # Use the default cache setting if one was not provided
        if cache is None:
            cache = self.CACHE

        # The _request method is a custom astroquery wrapper around the requests.request function that provides important astroquery-specific utility, including caching, HTTP header generation, progressbars, and local writing-to-disk.
        # This needs to be updated to use TAP (TapPlus), and the request_payload formatted correctly
        # Execute the request (generic HTTP request, similar to requests.Session.request but with added caching-related tools)
        if table in ["ps", "pscomppars"]:
            tap = pyvo.dal.tap.TAPService(baseurl=self.URL_TAP)
            # construct query from table and request_payload (including format)
            tap_query = self._request_to_sql(request_payload)
            try:
                response = tap.search(query=tap_query, language='ADQL')  # Note that this returns a VOTable
            except Exception as err:
                raise InvalidQueryError(str(err))
        else:
            response = self._request(
                "GET", self.URL_API, params=request_payload, timeout=self.TIMEOUT, cache=cache,
            )
            response.requested_format = criteria["format"]

        return response

    # This is the region query method
    @class_or_instance
    def query_region_async(self, table, coordinates, radius, *, get_query_payload=False, cache=None,
                           **criteria):
        """
        Filter a table using a cone search around specified coordinates

        Parameters
        ----------
        table : str
            The name of the table to query. A list of the tables on the Exoplanet Archive can be
            found on the documentation pages [1]_, [2]_.
        coordinates : str or `~astropy.coordinates`
            The coordinates around which to query.
        radius : str or `~astropy.units.Quantity`
            The radius of the cone search. Assumed to be have units of degrees if not provided as
            a ``Quantity``.
        get_query_payload : bool, optional
            Just return the dict of HTTP request parameters. Defaults to ``False``.
        cache : bool, optional
            Should the request result be cached? This can be useful for large repeated queries,
            but since the data in the archive is updated regularly, this defaults to ``False``.
        **criteria
            Any other filtering criteria to apply. These are described in detail in the archive
            documentation [1]_,[2]_ but some examples include ``select="*"`` to return all columns of
            the queried table or ``where=pl_name='K2-18 b'`` to filter a specific column.

        Returns
        -------
        response : `requests.Response`
            The HTTP response returned from the service.

        References
        ----------

        .. [1] `NASA Exoplanet Archive TAP Documentation
           <https://exoplanetarchive.ipac.caltech.edu/docs/TAP/usingTAP.html>`_
        .. [2] `NASA Exoplanet Archive API Documentation
           <https://exoplanetarchive.ipac.caltech.edu/docs/program_interfaces.html>`_
        """
        # Checks if coordinate strings is parsable as an astropy.coordinates object
        coordinates = commons.parse_coordinates(coordinates)

        # if radius is just a number we assume degrees
        if isinstance(radius, (int, float)):
            radius = radius * u.deg
        radius = coord.Angle(radius)

        criteria["ra"] = coordinates.ra.deg
        criteria["dec"] = coordinates.dec.deg
        criteria["radius"] = "{0} degree".format(radius.deg)

        # Runs the query method defined above, but with added region filter
        return self.query_criteria_async(
            table, get_query_payload=get_query_payload, cache=cache, **criteria,
        )

    # This method queries for a specific object in `exoplanets`, `compositepars`, or `exomultpars` tables.
    # Needs to be updated
    @class_or_instance
    def query_object_async(self, object_name, *, table="ps", get_query_payload=False,
                           cache=None, regularize=True, **criteria):
        """
        Search the tables of confirmed exoplanets for information about a planet or planet host

        The tables available to this query are the following (more information can be found on
        the archive's documentation pages [1]_):

        - ``ps``: This table compiles parameters derived from a multiple published
          references on separate rows, each row containing self-consistent values from one reference.
        - ``pscomppars``: This table compiles all parameters of confirmed exoplanets from multiple,
          published references in one row (not all self-consistent) per object.

        Parameters
        ----------
        object_name : str
            The name of the planet or star.  If ``regularize`` is ``True``, an attempt will be made
            to regularize this name using the ``aliastable`` table. Defaults to ``True``.
        table : [``"ps"`` or ``"pscomppars"``], optional
            The table to query, must be one of the supported tables: ``"ps"`` or ``"exomultpars"``.
            Defaults to ``"ps"``.
        get_query_payload : bool, optional
            Just return the dict of HTTP request parameters. Defaults to ``False``.
        cache : bool, optional
            Should the request result be cached? This can be useful for large repeated queries,
            but since the data in the archive is updated regularly, this defaults to ``False``.
        regularize : bool, optional
            If ``True``, the ``aliastable`` will be used to regularize the target name.
        **criteria
            Any other filtering criteria to apply. Values provided using the ``where`` keyword will
            be ignored.

        Returns
        -------
        response : `requests.Response`
            The HTTP response returned from the service.

        References
        ----------

        .. [1] `NASA Exoplanet Archive TAP Documentation
           <https://exoplanetarchive.ipac.caltech.edu/docs/TAP/usingTAP.html>`_
        .. [2] `NASA Exoplanet Archive API Documentation
           <https://exoplanetarchive.ipac.caltech.edu/docs/program_interfaces.html>`_
        """
        # if table.lower() in ["ps"]: # actually want to check if default was used, but wasn't working ...
        #     warnings.warn("The default table for this query method has changed after Archive 2.0 release. The ``ps`` table is being used, and is likely to return multiple rows for an object query. See https://exoplanetarchive.ipac.caltech.edu/docs/API_PS_columns.html", InputWarning, )

        prefix = OBJECT_TABLES.get(table, None)
        if prefix is None:
            raise InvalidQueryError(
                "Invalid table '{0}'. The allowed options are: {1}".format(
                    table, OBJECT_TABLES.keys()
                )
            )

        if regularize:
            object_name = self._regularize_object_name(object_name)

        if "where" in criteria:
            warnings.warn(
                "Any filters using the 'where' argument are ignored in ``query_object``. Consider using ``query_criteria`` instead.",
                InputWarning,
            )
        if table in ["ps", "pscomppars"]:
            criteria["where"] = "hostname='{1}' OR {0}name='{1}'".format(prefix, object_name.strip())
        else:
            criteria["where"] = "{0}hostname='{1}' OR {0}name='{1}'".format(prefix, object_name.strip())

        return self.query_criteria_async(
            table, get_query_payload=get_query_payload, cache=cache, **criteria,
        )

    # This should stay the same for now
    @class_or_instance
    def query_aliases(self, object_name, *, cache=None):
        """
        Search for aliases for a given confirmed planet or planet host

        Parameters
        ----------
        object_name : str
            The name of a planet or star to regularize using the ``aliastable`` table.
        cache : bool, optional
            Should the request result be cached? This can be useful for large repeated queries,
            but since the data in the archive is updated regularly, this defaults to ``False``.

        Returns
        -------
        response : list
            A list of aliases found for the object name. The default name will be listed first.
        """
        return list(
            self.query_criteria(
                "aliastable", objname=object_name.strip(), cache=cache, format="csv"
            )["aliasdis"]
        )

    @class_or_instance
    def _regularize_object_name(self, object_name):
        """Regularize the name of a planet or planet host using the ``aliastable`` table"""
        try:
            aliases = self.query_aliases(object_name, cache=False)
        except RemoteServiceError:
            aliases = []
        if aliases:
            return aliases[0]
        warnings.warn("No aliases found for name: '{0}'".format(object_name), NoResultsWarning)
        return object_name

    # Look for response errors. This might need to be updated for TAP
    def _handle_error(self, text):
        """
        Parse the response from a request to see if it failed

        Parameters
        ----------
        text : str
            The decoded body of the response.

        Raises
        ------
        InvalidColumnError :
            If ``select`` included an invalid column.
        InvalidTableError :
            If the queried ``table`` does not exist.
        RemoteServiceError :
            If anything else went wrong.
        """
        # Error messages will always be formatted starting with the word "ERROR"
        if not text.startswith("ERROR"):
            return

        # Some errors have the form:
        #   Error type: ...
        #   Message: ...
        # so we'll parse those to try to provide some reasonable feedback to the user
        error_type = None
        error_message = None
        for line in text.replace("<br>", "").splitlines():
            match = re.search(r"Error Type:\s(.+)$", line)
            if match:
                error_type = match.group(1).strip()
                continue

            match = re.search(r"Message:\s(.+)$", line)
            if match:
                error_message = match.group(1).strip()
                continue

        # If we hit this condition, that means that we weren't able to parse the error so we'll
        # just throw the full response
        if error_type is None or error_message is None:
            raise RemoteServiceError(text)

        # A useful special is if a column name is unrecognized. This has the format
        #   Error type: SystemError
        #   Message: ... "NAME_OF_COLUMN": invalid identifier ...
        if error_type.startswith("SystemError"):
            match = re.search(r'"(.*)": invalid identifier', error_message)
            if match:
                raise InvalidQueryError(
                    (
                        "'{0}' is an invalid identifier. This error can be caused by invalid "
                        "column names, missing quotes, or other syntax errors"
                    ).format(match.group(1).lower())
                )

        elif error_type.startswith("UserError"):
            # Another important one is when the table is not recognized. This has the format:
            #   Error type: UserError - "table" parameter
            #   Message: ... "NAME_OF_TABLE" is not a valid table.
            match = re.search(r'"(.*)" is not a valid table', error_message)
            if match:
                raise InvalidTableError("'{0}' is not a valid table".format(match.group(1).lower()))

            raise InvalidQueryError("{0}\n{1}".format(error_type, error_message))

        # Finally just return the full error message if we got here
        message = "\n".join(line for line in (error_type, error_message) if line is not None)
        raise RemoteServiceError(message)

    def _fix_units(self, data):
        """
        Fix any undefined units using a set of hacks

        Parameters
        ----------
        data : `~astropy.table.Table`
            The original data table without units.

        Returns
        -------
        new_data : `~astropy.table.QTable` or `~astropy.table.Table`
            The original ``data`` table with units applied where possible.
        """

        # To deal with masked data and quantities properly, we need to construct the QTable
        # manually so we'll loop over the columns and process each one independently
        column_names = list(data.columns)
        column_data = []
        column_masks = dict()
        for col in column_names:
            unit = data[col].unit
            unit = UNIT_MAPPER.get(str(unit), unit)
            try:
                data[col].mask = False
            except AttributeError:
                pass
            # Columns with dtype==object/str can't have units according to astropy
            # Set unit to None
            if data[col].dtype == object and unit is not None:
                unit = None
                data[col] = data[col].astype(str)
            if data[col].dtype == object and unit is None:
                data[col] = data[col].astype(str)
            if isinstance(unit, u.UnrecognizedUnit):
                # some special cases
                unit_str = str(unit).lower()
                if unit_str == "earth" and "prad" in col:
                    unit = u.R_earth
                elif unit_str == "solar" and "radius" in col.lower():
                    unit = u.R_sun
                elif unit_str == "solar" and "mass" in col.lower():
                    unit = u.M_sun
                elif (
                    col.startswith("mlmag")
                    or col.startswith("mlext")
                    or col.startswith("mlcol")
                    or col.startswith("mlred")
                ):
                    unit = u.mag

                else:  # pragma: nocover
                    warnings.warn("Unrecognized unit: '{0}'".format(unit), AstropyWarning)

            # Unmask since astropy doesn't like masked values in columns with units
            try:
                column_masks[col] = data[col].mask
            except AttributeError:
                pass
            else:
                column_masks[col] = False

            data[col].unit = unit
            column_data.append(data[col])

        # Build the new `QTable` and copy over the data masks if there are any
        result = QTable(column_data, names=column_names, masked=len(column_masks) > 0)
        for key, mask in column_masks.items():
            result[key].mask = mask

        return result

    def _parse_result(self, response, verbose=False):
        """
        Parse the result of a `~requests.Response` (from API) or `pyvo.dal.tap.TAPResults` (from TAP) object
        and return an `~astropy.table.Table`

        Parameters
        ----------
        response : `~requests.Response` or `pyvo.dal.tap.TAPResults`
            The response from the server.
        verbose : bool
            Currently has no effect.

        Returns
        -------
        data : `~astropy.table.Table` or `~astropy.table.QTable`
        """

        if isinstance(response, pyvo.dal.tap.TAPResults):
            data = response.to_table()
            # TODO: implement format conversion for TAP return
        else:
            # Extract the decoded body of the response
            text = response.text

            # Raise an exception if anything went wrong
            self._handle_error(text)

            # Parse the requested format to figure out how to parse the returned data.
            fmt = response.requested_format.lower()
            if "ascii" in fmt or "ipac" in fmt:
                data = ascii.read(text, format="ipac", fast_reader=False, converters=CONVERTERS)
            elif "csv" in fmt:
                data = ascii.read(text, format="csv", fast_reader=False, converters=CONVERTERS)
            elif "bar" in fmt or "pipe" in fmt:
                data = ascii.read(text, fast_reader=False, delimiter="|", converters=CONVERTERS)
            elif "xml" in fmt or "table" in fmt:
                data = parse_single_table(io.BytesIO(response.content)).to_table()
            else:
                data = ascii.read(text, fast_reader=False, converters=CONVERTERS)

        # Fix any undefined units
        data = self._fix_units(data)

        # For backwards compatibility, add a `sky_coord` column with the coordinates of the object
        # if possible
        if "ra" in data.columns and "dec" in data.columns:
            data["sky_coord"] = SkyCoord(ra=data["ra"], dec=data["dec"], unit=u.deg)

        if not data:
            warnings.warn("Query returned no results.", NoResultsWarning)

        return data

    def _handle_all_columns_argument(self, **kwargs):
        """
        Deal with the ``all_columns`` argument that was exposed by earlier versions

        This method will warn users about this deprecated argument and update the query syntax
        to use ``select='*'``.
        """
        # We also have to manually pop these arguments from the dict because
        # `deprecated_renamed_argument` doesn't do that for some reason for all supported astropy
        # versions (v3.1 was beheaving as expected)
        kwargs.pop("show_progress", None)
        kwargs.pop("table_path", None)

        # Deal with `all_columns` properly
        if kwargs.pop("all_columns", None):
            kwargs["select"] = kwargs.get("select", "*")

        return kwargs

    @class_or_instance
    def _request_to_sql(self, request_payload):
        """Convert request_payload dict to SQL query string to be parsed by TAP."""

        # Required minimum query string
        query_req = "select {0} from {1}".format(request_payload.pop("select", "*"), request_payload.pop("table", None))
        if "order" in request_payload.keys():
            request_payload["order by"] = request_payload.pop("order")
        if "format" in request_payload.keys():
            responseformat = request_payload.pop("format")  # figure out what to do with the format keyword
        if "ra" in request_payload.keys():  # means this is a `query_region` call
            request_payload["where"] = "contains(point('icrs',ra,dec),circle('icrs',{0},{1},{2}))=1".format(request_payload["ra"], request_payload["dec"], request_payload["radius"])
            del request_payload["ra"]
            del request_payload["dec"]
            del request_payload["radius"]
        if "where" in request_payload:
            if "pl_hostname" in request_payload["where"]:  # means this is a `query_object`
                request_payload["where"] = "pl_hostname or pl_name like {0}".format(request_payload["where"][request_payload["where"].find("=")+2:request_payload["where"].find("OR")-2])  # This is a bit hacky since we are getting this from the request_payload (downstream) instead of directly from object_name
        query_opt = " ".join("{0} {1}".format(key, value) for key, value in request_payload.items())
        tap_query = "{0} {1}".format(query_req, query_opt)

        return tap_query

    # Could we use similar @deprecated decorator for new changes and warnings?
    @deprecated(since="v0.4.1", alternative="query_object")
    @deprecated_renamed_argument(["show_progress", "table_path"],
                                 [None, None], "v0.4.1", arg_in_kwargs=True)
    def query_planet(self, planet_name, cache=None, regularize=True, **criteria):
        """
        Search the ``exoplanets`` table for a confirmed planet

        Parameters
        ----------
        planet_name : str
            The name of a confirmed planet. If ``regularize`` is ``True``, an attempt will be made
            to regularize this name using the ``aliastable`` table.
        cache : bool, optional
            Should the request result be cached? This can be useful for large repeated queries,
            but since the data in the archive is updated regularly, this defaults to ``False``.
        regularize : bool, optional
            If ``True``, the ``aliastable`` will be used to regularize the target name.
        **criteria
            Any other filtering criteria to apply. Values provided using the ``where`` keyword will
            be ignored.
        """
        if regularize:
            planet_name = self._regularize_object_name(planet_name)
        criteria = self._handle_all_columns_argument(**criteria)
        criteria["where"] = "pl_name='{0}'".format(planet_name.strip())
        return self.query_criteria("exoplanets", cache=cache, **criteria)

    @deprecated(since="v0.4.1", alternative="query_object")
    @deprecated_renamed_argument(["show_progress", "table_path"],
                                 [None, None], "v0.4.1", arg_in_kwargs=True)
    def query_star(self, host_name, cache=None, regularize=True, **criteria):
        """
        Search the ``exoplanets`` table for a confirmed planet host

        Parameters
        ----------
        host_name : str
            The name of a confirmed planet host. If ``regularize`` is ``True``, an attempt will be
            made to regularize this name using the ``aliastable`` table.
        cache : bool, optional
            Should the request result be cached? This can be useful for large repeated queries,
            but since the data in the archive is updated regularly, this defaults to ``False``.
        regularize : bool, optional
            If ``True``, the ``aliastable`` will be used to regularize the target name.
        **criteria
            Any other filtering criteria to apply. Values provided using the ``where`` keyword will
            be ignored.
        """
        if regularize:
            host_name = self._regularize_object_name(host_name)
        criteria = self._handle_all_columns_argument(**criteria)
        criteria["where"] = "pl_hostname='{0}'".format(host_name.strip())
        return self.query_criteria("exoplanets", cache=cache, **criteria)


NasaExoplanetArchive = NasaExoplanetArchiveClass()
