# Licensed under a 3-clause BSD style license - see LICENSE.rst

# 1. standard library imports
from io import BytesIO, StringIO
from six.moves.urllib.parse import unquote
import time
from xml.etree import ElementTree
from datetime import datetime, timezone

# 2. third party imports
import astropy.units as u
import astropy.coordinates as coord
from astropy.table import Table
from astropy.io.votable import parse

# 3. local imports - use relative imports
# commonly required local imports shown below as example
# all Query classes should inherit from BaseQuery.
from ..query import BaseQuery
# has common functions required by most modules
from ..utils import commons
# prepend_docstr is a way to copy docstrings between methods
from ..utils import prepend_docstr_nosections
# async_to_sync generates the relevant query tools from _async methods
from ..utils import async_to_sync
# import configurable items declared in __init__.py
from . import conf

# export all the public classes and methods
__all__ = ['Casda', 'CasdaClass']


@async_to_sync
class CasdaClass(BaseQuery):

    """
    Class for accessing ASKAP data through the CSIRO ASKAP Science Data Archive (CASDA). Typical usage:

    result = Casda.query_region('22h15m38.2s -45d50m30.5s', radius=0.5 * u.deg)
    """
    # use the Configuration Items imported from __init__.py to set the URL,
    # TIMEOUT, etc.
    URL = conf.server
    TIMEOUT = conf.timeout
    _soda_base_url = conf.soda_base_url
    _uws_ns = {'uws': 'http://www.ivoa.net/xml/UWS/v1.0'}

    def __init__(self, user=None, password=None):
        super(CasdaClass, self).__init__()
        if user is None:
            self._authenticated = False
        else:
            self._authenticated = True
            # self._user = user
            # self._password = password
            self._auth = (user, password)


    def query_region_async(self, coordinates, radius=None, height=None, width=None,
                           get_query_payload=False, cache=True):
        """
        Queries a region around the specified coordinates. Either a radius or both a height and a width must be provided.

        Parameters
        ----------
        coordinates : str or `astropy.coordinates`.
            coordinates around which to query
        radius : str or `astropy.units.Quantity`.
            the radius of the cone search
        width : str or `astropy.units.Quantity`
            the width for a box region
        height : str or `astropy.units.Quantity`
            the height for a box region
        get_query_payload : bool, optional
            Just return the dict of HTTP request parameters.
        cache: bool, optional
            Use the astroquery internal query result cache

        Returns
        -------
        response : `requests.Response`
            The HTTP response returned from the service.
            All async methods should return the raw HTTP response.
        """
        request_payload = self._args_to_payload(coordinates=coordinates, radius=radius, height=height,
                                                width=width)
        if get_query_payload:
            return request_payload

        response = self._request('GET', self.URL, params=request_payload,
                                 timeout=self.TIMEOUT, cache=cache)

        # result = self._parse_result(response)
        return response

    # Create the dict of HTTP request parameters by parsing the user
    # entered values.
    def _args_to_payload(self, **kwargs):
        request_payload = dict()

        # Convert the coordinates to FK5
        coordinates = kwargs.get('coordinates')
        c = commons.parse_coordinates(coordinates).transform_to(coord.FK5)

        if kwargs['radius'] is not None:
            radius = u.Quantity(kwargs['radius']).to(u.deg)
            pos = 'CIRCLE {} {} {}'.format(c.ra.degree, c.dec.degree, radius.value)
        elif kwargs['width'] is not None and kwargs['height'] is not None:
            width = u.Quantity(kwargs['width']).to(u.deg).value
            height = u.Quantity(kwargs['height']).to(u.deg).value
            top = c.dec.degree - (height/2)
            bottom = c.dec.degree + (height/2)
            left = c.ra.degree - (width/2)
            right = c.ra.degree + (width/2)
            pos = 'RANGE {} {} {} {}'.format(left, right, top, bottom)
        else:
            raise ValueError("Either 'radius' or both 'height' and 'width' must be supplied.")

        request_payload['POS'] = pos

        return request_payload

    # the methods above implicitly call the private _parse_result method.
    # This should parse the raw HTTP response and return it as
    # an `astropy.table.Table`.
    def _parse_result(self, response, verbose=False):
        # if verbose is False then suppress any VOTable related warnings
        if not verbose:
            commons.suppress_vo_warnings()
        # try to parse the result into an astropy.Table, else
        # return the raw result with an informative error message.
        try:
            # do something with regex to get the result into
            # astropy.Table form. return the Table.
            data = BytesIO(response.content)
            table = Table.read(data)
            return table
        except ValueError as e:
            # catch common errors here, but never use bare excepts
            # return raw result/ handle in some way
            print("Failed to convert query result to table", e)
            return response

    def filter_out_unreleased(self, table):
        """
        Return a subset of the table which only includes released (public) data.
        :param table: A table of results as returned by query_region. Must include an obs_release_date column.
        :return The table with all unreleased (non public) data products filtered out.
        """
        now = str(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f'))
        return table[(table['obs_release_date'] != '') & (table['obs_release_date'] < now)]

    def stage_data(self, table):
        """
        Request access to a set of data files. All requests for data must use authentication. If you have access to the
        data, the requested files will be brought online and a set of URLs to download the files will be returned.

        :param table: A table describing the files to be staged, such as produced by query_region. It must include an
                      access_url column.
        :return: A list of urls of both the requested files and the checksums for the files
        """
        if not self._authenticated:
            raise ValueError("Credentials must be supplied to download CASDA image data")

        # Use datalink to get authenticated access for each file
        tokens = []
        for row in table:
            access_url = row['access_url']
            response = self._request('GET', access_url, auth=self._auth,
                                     timeout=self.TIMEOUT)
            soda_url, id_token = self._parse_datalink_for_service_and_id(response, 'cutout_service')
            tokens.append(id_token)

        # Create job to stage all files
        job_url = self._create_soda_job(tokens, soda_url=soda_url)
        print("Created data staging job", job_url)

        # Wait for job to be complete
        final_status = self._run_job(job_url)
        print("Job ended with status", final_status)

        # Build lost of result file urls
        job_details = self._get_job_details_xml(job_url)
        fileurls = []
        for result in job_details.find("uws:results", self._uws_ns).findall("uws:result", self._uws_ns):
            file_location = unquote(result.get("{http://www.w3.org/1999/xlink}href"))
            fileurls.append(file_location)

        return fileurls

    def _parse_datalink_for_service_and_id(self, response, service_name):
        """ Parses a datalink file into a vo table, and returns the async service url and the authenticated id token """
        # Parse the datalink file into a vo table, and get the results
        data = BytesIO(response.content)
        #votable = Table.read(data) #, pedantic=False)
        #f = StringIO(response)
        votable = parse(data, pedantic=False)
        results = next(resource for resource in votable.resources if
                       resource.type == "results")
        if results is None:
            return None
        results_array = results.tables[0].array
        async_url = None
        authenticated_id_token = None

        # Find the authenticated id token for accessing the image cube
        for x in results_array:
            if x['service_def'].decode("utf8") == service_name:
                authenticated_id_token = x['authenticated_id_token']

        # Find the async url
        for x in votable.resources:
            if x.type == "meta":
                if x.ID == service_name:
                    for p in x.params:
                        if p.name == "accessURL":
                            async_url = p.value

        # print "Async url:", async_url
        # print "Authenticated id token for async access:", authenticated_id_token

        return async_url, authenticated_id_token

    def _create_soda_job(self, authenticated_id_tokens, soda_url=None):
        """ Creates the async job, returning the url to query the job status and details """
        id_params = list(
            map((lambda authenticated_id_token: ('ID', authenticated_id_token)),
                authenticated_id_tokens))
        async_url = soda_url if soda_url else self._get_soda_url()

        resp = self._request('POST', async_url, params=id_params, cache=False)
        return resp.url

    def _run_job(self, job_location, poll_interval=20):
        """
        Start an async job (e.g. TAP or SODA) and wait for it to be completed.

        :param job_location: The url to query the job status and details
        :param poll_interval: The number of seconds to wait between checks on the status of the job.
        :return: The single word status of the job. Normally COMPLETED or ERROR
        """

        # Start the async job
        print("Starting the retrieval job...")
        self._request('POST', job_location + "/phase", data={'phase': 'RUN'}, cache=False)

        # Poll until the async job has finished
        prev_status = None
        count = 0
        job_details = self._get_job_details_xml(job_location)
        status = self._read_job_status(job_details)
        while status == 'EXECUTING' or status == 'QUEUED' or status == 'PENDING':
            count += 1
            if True: # status != prev_status:# or count > 10:
                print("Job is %s, polling every %d seconds." % (status, poll_interval))
                count = 0
                prev_status = status
            time.sleep(poll_interval)
            job_details = self._get_job_details_xml(job_location)
            status = self._read_job_status(job_details)
            print (status)
        return status

    def _get_soda_url(self):
        return self._soda_base_url + "data/async"

    def _get_job_details_xml(self, async_job_url):
        """ Get job details as XML """
        response = self._request('GET', async_job_url, cache=False)
        job_response = response.text
        return ElementTree.fromstring(job_response)

    def _read_job_status(self, job_details_xml, ns=_uws_ns):
        """ Read job status from the job details XML """
        status = job_details_xml.find("uws:phase", ns).text
        return status


# the default tool for users to interact with is an instance of the Class
Casda = CasdaClass()
