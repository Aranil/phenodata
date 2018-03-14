# -*- coding: utf-8 -*-
# (c) 2018 Andreas Motl <andreas@hiveeyes.org>
import attr
import logging
import pandas as pd
from datetime import datetime
from phenodata.util import haversine_distance

logger = logging.getLogger(__name__)

@attr.s
class DwdPhenoData(object):
    """
    Conveniently access phenology information from the Climate Data Center (CDC)
    FTP server operated by »Deutscher Wetterdienst« (DWD).

    See also `cdc-readme`_:
    Phenological data is collected at about 1200 active stations. The state of development of
    selected plants (e.g., apple, birch, snow drops, goose berry, wheat, vine, etc.) is reported
    by annual reporters and immediate reporters.

    The lists of phenological stations can be found in the list of phenology `annual reporters`_
    and the list of phenology `immediate reporters`_.

    .. _cdc-readme: ftp://ftp-cdc.dwd.de/pub/CDC/Readme_intro_CDC_ftp.txt
    .. _annual reporters: ftp://ftp-cdc.dwd.de/pub/CDC/help/PH_Beschreibung_Phaenologie_Stationen_Jahresmelder.txt
    .. _immediate reporters: ftp://ftp-cdc.dwd.de/pub/CDC/help/PH_Beschreibung_Phaenologie_Stationen_Sofortmelder.txt

    """

    # Instance of the lowlevel DWD CDC FTP server client wrapper object ``phenodata.dwd.cdc.DwdCdcClient``
    cdc = attr.ib()

    # The dataset to access, either "annual" or "immediate"
    dataset = attr.ib()

    @property
    def data_directory(self):
        """
        Location of observations on the FTP server
        """
        return '/observations_germany/phenology/{dataset}_reporters'.format(dataset=self.dataset)

    def get_species(self):
        """
        Return DataFrame with species information
        """
        return self.cdc.get_dataframe(path='/help/PH_Beschreibung_Pflanze.txt', index_column=0)

    def get_phases(self):
        """
        Return DataFrame with phases information
        """
        return self.cdc.get_dataframe(path='/help/PH_Beschreibung_Phase.txt', index_column=0)

    def get_quality_levels(self):
        """
        Return DataFrame with quality level information
        """
        return self.cdc.get_dataframe(path='/help/PH_Beschreibung_Phaenologie_Qualitaetsniveau.txt', index_column=0)

    def get_quality_bytes(self):
        """
        Return DataFrame with quality bytes information
        """
        return self.cdc.get_dataframe(path='/help/PH_Beschreibung_Phaenologie_Qualitaetsbyte.txt', index_column=0)

    def get_stations(self, all=False):
        """
        Return DataFrame with stations information.
        """

        if self.dataset == 'immediate':
            filename = '/help/PH_Beschreibung_Phaenologie_Stationen_Sofortmelder.txt'
        elif self.dataset == 'annual':
            filename = '/help/PH_Beschreibung_Phaenologie_Stationen_Jahresmelder.txt'
        else:
            raise KeyError('Unknown dataset "{}"'.format(self.dataset))

        # Read stations CSV file
        data = self.cdc.get_dataframe(path=filename, index_column=0)

        # Unless "all==True", use only rows with "Datum Stationsaufloesung" == nan
        if not all:
            data = data[data['Datum Stationsaufloesung'].isna()]

        # Appropriately coerce geolocation values to float
        #dataframe_coerce_columns(data, ['geograph.Breite', 'geograph.Laenge'], float)

        return data

    def nearest_station(self, latitude, longitude, all=False):
        """
        Select most current stations datasets.

        Stolen from https://github.com/marians/dwd-weather
        """
        closest = None
        closest_distance = 99999999999
        for index, station in self.get_stations(all=all).iterrows():
            d = haversine_distance((longitude, latitude),
                (station["geograph.Laenge"], station["geograph.Breite"]))
            if d < closest_distance:
                closest = station
                closest_distance = d
        return closest.to_frame()

    def nearest_stations(self, latitude, longitude, all=False, limit=10):
        """
        Select most current stations datasets.

        Stolen from https://github.com/marians/dwd-weather
        """

        # Retrieve stations
        stations = self.get_stations(all=all)

        # Build list of distances to corresponding station
        distances = []
        for index, station in stations.iterrows():
            distance = haversine_distance(
                (longitude, latitude),
                (station['geograph.Laenge'], station['geograph.Breite'])
            )
            distances.append(distance)

        # Insert list of distances as new column
        stations.insert(1, 'Distanz', distances)

        # Sort ascending by distance value and limit row count
        stations = stations.sort_values('Distanz').head(n=limit)

        # Convert Series to DataFrame again
        frame = pd.DataFrame(stations)

        return frame

    def get_observations(self, options):
        """
        Retrieve observations.

        - Obtain query options
        - Compute DataFrame with combined observation data
        - Apply a bunch of filters to the result data
        """

        # Acquire data
        observations = self.query(partition=options['partition'], files=options['filename'])

        # Filter data
        observations = self.flux(observations, criteria=options)

        return observations

    def get_forecast(self, options):
        """
        Forecast observations.

        - Obtain query options
        - Get real observations, all filtering options can be used
        - Group results by (Stations_id, Objekt_id, Phase_id)
        - Aggregate mean "day of the year" value of the "Jultag" values for each group
        """

        # Get current observations
        data = self.get_observations(options)

        # Group by station, species and phase
        # https://pandas.pydata.org/pandas-docs/stable/groupby.html
        grouped = data.groupby(['Stations_id', 'Objekt_id', 'Phase_id'])

        # Aggregate mean "day of the year" value of the "Jultag" values for each group
        series = grouped['Jultag'].mean().round().astype(int)

        # Convert Series to DataFrame
        frame = series.to_frame()

        # Compute ISO date from "day of the year" values
        real_dates = pd.to_datetime(datetime.today().year * 1000 + frame['Jultag'], format='%Y%j')
        frame.insert(0, 'Datum', real_dates)

        return frame

    def query(self, partition=None, files=None):
        """
        The FTP/Pandas workhorse, converges data from multiple observation data
        CSV files on upstream CDC FTP server into a single Pandas DataFrame object.

        - Obtains ``partition`` parameter which can be either ``annual`` or ``immediate``.
        - Obtains optional ``files`` parameter which will be applied
          as an "include" filter to the list of scanned file names.
        """

        logger.info('Starting data acquisition')

        # Search FTP server
        paths = self.scan_files(partition, include=files, field='url')

        # The main DataFrame object
        results = pd.DataFrame()

        # Load multiple files into single DataFrame
        for path in paths:

            # Skip invalid files
            if 'Kulturpflanze_Ruebe_akt' in path:
                logger.warning('Skipping file "{}" due to invalid header format (all caps)'.format(path))
                continue

            # Acquire DataFrame from CSV data
            data = self.cdc.get_dataframe(path, coerce_int=True)

            # Sanity checks
            if data is None:
                continue

            # Coerce "Eintrittsdatum" column into date format
            data['Eintrittsdatum'] = pd.to_datetime(data['Eintrittsdatum'], errors='coerce', format='%Y%m%d')

            # Append to DataFrame
            results = results.append(data)

        # Sanity checks
        if results.empty:
            logger.info('Querying DWD CDC returned empty results')
            return

        # Reset index column
        results.reset_index(drop=True, inplace=True)

        return results

    def create_megaframe(self, frame):

        # https://pandas.pydata.org/pandas-docs/stable/merging.html#database-style-dataframe-joining-merging

        # Stations_id
        frame = pd.merge(frame, self.get_stations(), left_on='Stations_id', right_index=True)

        # Objekt_id
        frame = pd.merge(frame, self.get_species(), left_on='Objekt_id', right_index=True)

        # Phase_id
        frame = pd.merge(frame, self.get_phases(), left_on='Phase_id', right_index=True)

        # Qualitaetsniveau
        frame = pd.merge(frame, self.get_quality_levels(), left_on='Qualitaetsniveau', right_index=True)

        # Eintrittsdatum_QB
        frame = pd.merge(frame, self.get_quality_bytes(), left_on='Eintrittsdatum_QB', right_index=True)

        #print frame.to_csv(encoding='utf-8')
        #sys.exit()
        return frame


    def humanize_megaframe(self, frame, language=None):

        canvas = pd.DataFrame()

        # Which fields to use from "station" entity
        station_fields = ['Stationsname', 'Naturraumgruppe', 'Naturraum', 'Bundesland']

        # Improved map for quality level texts
        quality_level_text = {
             1: u'Loadtime checks',
             7: u'ROUTKLI checks',
            10: u'ROUTKLI checks, corrected',
        }

        # Which field to choose from the "species" entity. One of "Objekt", "Objekt_englisch", "Objekt_latein".
        # Which field to choose from the "phase" entity. One of "Phase", "Phase_englisch".
        species_field = 'Objekt_englisch'
        phase_field = 'Phase_englisch'
        if language:
            language = language.lower()
            if language == 'german':
                species_field = 'Objekt'
                phase_field = 'Phase'
                quality_level_text = {
                     1: u'Vorabprüfung beim Laden',
                     7: u'ROUTKLI Prüfung',
                    10: u'ROUTKLI Prüfung, korrigiert',
                }
            elif language == 'latin':
                species_field = 'Objekt_latein'

        stations = []
        species = []
        phases = []
        quality_levels = []
        quality_bytes = []
        for index, row in frame.iterrows():

            # Station
            station_parts = [row.get(field, '') for field in station_fields]
            station_label = ', '.join(station_parts)
            station_label += ' [{}]'.format(row['Stations_id'])
            stations.append(station_label)

            # Species
            species_label = row.get(species_field, '')
            species_label += ' [{}]'.format(row['Objekt_id'])
            species.append(species_label)

            # Phase
            phase_label = row.get(phase_field, '')
            phase_label += ' [{}]'.format(row['Phase_id'])
            phases.append(phase_label)

            # Qualitaetsniveau
            ql_label = quality_level_text.get(row['Qualitaetsniveau'], row.get('Beschreibung_x', ''))
            ql_label += ' [{}]'.format(row['Qualitaetsniveau'])
            quality_levels.append(ql_label)

            # Eintrittsdatum_QB
            qb_label = row.get('Beschreibung_y', '')
            qb_label += ' [{}]'.format(row['Eintrittsdatum_QB'])
            quality_bytes.append(qb_label)

        # Build fresh DataFrame with designated order of columns
        canvas['Jahr'] = frame['Referenzjahr']
        canvas['Datum'] = frame['Eintrittsdatum']
        canvas['Spezies'] = species
        canvas['Phase'] = phases
        canvas['Station'] = stations
        canvas['QS-Level'] = quality_levels
        canvas['QS-Byte'] = quality_bytes

        return canvas

    def flux(self, results, criteria=None):
        """
        The flux compensator. All filtering on the DataFrame takes places here.
        """

        logger.info('Entering flux compensator: Filter and transform data')

        criteria = criteria or {}

        # Sanity checks
        if results is None:
            return

        # Filter DataFrame
        # https://pythonspot.com/pandas-filter/
        # https://stackoverflow.com/questions/12065885/filter-dataframe-rows-if-value-in-column-is-in-a-set-list-of-values/12065904#12065904

        # Build "boolean indexing" filter expression from multiple criteria
        # https://pandas.pydata.org/pandas-docs/stable/indexing.html#boolean-indexing
        isin_map = {
            'year': results.Referenzjahr,
            'quality-level': results.Qualitaetsniveau,
            'quality-byte': results.Eintrittsdatum_QB,
            'station-id': results.Stations_id,
            'species-id': results.Objekt_id,
            'phase-id': results.Phase_id,
        }

        # Lowlevel filtering based on IDs
        # For each designated field, add ``.isin`` criteria to "boolean index" expression
        expression = True
        for key, reference in isin_map.items():
            if criteria[key]:
                expression &= reference.isin(criteria[key])


        # Humanized filtering based on merged/joined DataFrames
        # TODO: For each designated field, add ``.str.contains('|'.join(patterns)`` criteria to "boolean index" expression
        # https://stackoverflow.com/questions/12065885/filter-dataframe-rows-if-value-in-column-is-in-a-set-list-of-values/26724725#26724725


        # Apply filter expression to DataFrame
        if type(expression) is not bool:
            results = results[expression]

        return results

    def scan_files(self, partition, include=None, field=None):
        """
        Scan upstream files in three-level directory hierarchy.
        """

        # The full URL to the FTP data directory
        url = self.cdc.baseurl + self.data_directory

        # Query FTP server for files
        entries = self.cdc.ftp.scan_files(
            url, subdir=partition,
            include=include,
            include_base=['PH_(Sofort|Jahres)melder.+\.txt'],
            exclude_base=['PH_Beschreibung', 'Spezifizierung'],
        )

        # Return entries if projection to field not requested
        if not field:
            return entries

        # Project entries to results
        results = []
        for entry in entries:

            try:
                item = entry[field]
                results.append(item)

            except KeyError:
                raise KeyError('Projection "field={}" not available'.format(field))

        return results
