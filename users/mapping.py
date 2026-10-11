# """Process / Location mapping read from media/mapping/mapping.csv.

# The mapping file is the single source of truth for which ATS ID belongs to
# which Process and Location. Nothing here is hard-coded: the dropdown values on
# the upload page and the validation applied to an uploaded downtime file both
# come from this file, so adding a process or moving a site is a change to the
# mapping file alone.

# The file is read at most once per modification: the parsed result is cached and
# the cache is dropped as soon as the file's size or mtime changes, which keeps
# an upload request from re-parsing ~5k rows every time.
# """

# import logging
# import os
# import threading

# import pandas as pd
# from django.conf import settings

# logger = logging.getLogger(__name__)

# # Location of the mapping file. Overridable so a deployment can point elsewhere
# # without a code change.
# MAPPING_FILE = os.environ.get(
#     'MAPPING_FILE', os.path.join(settings.MEDIA_ROOT, 'mapping', 'mapping.csv')
# )

# # Header names accepted for each logical column, lower-cased and stripped of
# # spaces/underscores before comparison. The workbook currently ships
# # "EmpCode / Process / Location", but a re-export with slightly different
# # labels must not break the upload page.
# _ATS_HEADERS = (
#     'empcode', 'empcodes', 'empid', 'employeecode', 'employeeid',
#     'atsid', 'atsids', 'atscode', 'atsempcode', 'agentid',
# )
# _PROCESS_HEADERS = (
#     'process', 'processname', 'processes', 'campaign', 'campaignname',
#     'lob', 'lobname',
# )
# _LOCATION_HEADERS = (
#     'location', 'locationname', 'locations', 'site', 'sitename',
#     'city', 'branch', 'center', 'centre',
# )

# # Exact wording required by the business for a cross process/location upload.
# MISMATCH_ERROR = (
# "Upload file contains data for a different process or location.Please correct the file and re-upload it."
# )
# UNKNOWN_ATS_ERROR = "This ATSid is not active. Please verify and reupload."

# _cache = None          # dict returned by _load()
# _cache_stamp = None    # (size, mtime) of the workbook the cache was built from
# _lock = threading.Lock()


# class MappingUnavailable(Exception):
#     """Raised when the mapping file is missing or unusable.

#     Uploads are blocked rather than waved through: without the mapping there is
#     no way to tell whether the file belongs to the selected process/location.
#     """


# def _norm_header(name):
#     """Reduce a column header to a comparable token."""
#     return ''.join(str(name).split()).replace('_', '').replace('-', '').lower()


# def _norm_value(value):
#     """Case- and whitespace-insensitive form used for comparisons only.

#     Locations in the workbook are inconsistently cased ("Noida" vs
#     "CHANDIGARH"), so comparisons normalise while the value shown in the
#     dropdown stays exactly as the workbook spells it.
#     """
#     return ' '.join(str(value).split()).lower()


# def normalise_ats_id(value):
#     """Canonical form of an ATS ID: trimmed, upper-cased, no inner spaces."""
#     return ''.join(str(value).split()).upper()


# def _pick_column(columns, candidates, label):
#     """Find the workbook column matching one of `candidates`.

#     Falls back to a substring match so a header such as "Process Name (LOB)"
#     is still recognised.
#     """
#     normalised = {_norm_header(col): col for col in columns}

#     for candidate in candidates:
#         if candidate in normalised:
#             return normalised[candidate]

#     for norm, original in normalised.items():
#         if any(candidate in norm for candidate in candidates):
#             return original

#     raise MappingUnavailable(
#         "mapping file has no recognisable {} column. Columns found: {}".format(
#             label, list(columns)
#         )
#     )


# def _file_stamp(path):
#     stat = os.stat(path)
#     return (stat.st_size, stat.st_mtime_ns)


# def _read_mapping_frame(path):
#     """Read the mapping file, picking the reader from its extension.

#     The file is a CSV today and was a workbook before; the MAPPING_FILE override
#     may point at either, so the format is not assumed.
#     """
#     if os.path.splitext(path)[1].lower() == '.csv':
#         return pd.read_csv(path)
#     return pd.read_excel(path)


# def _load():
#     """Parse the mapping file into the cached structure. Caller holds the lock."""
#     if not os.path.exists(MAPPING_FILE):
#         raise MappingUnavailable("mapping file not found at %s" % MAPPING_FILE)

#     try:
#         df = _read_mapping_frame(MAPPING_FILE)
#     except Exception as exc:  # unreadable / corrupt mapping file
#         raise MappingUnavailable(
#             "mapping file could not be read: %s" % exc
#         ) from exc

#     if df.empty:
#         raise MappingUnavailable("mapping file is empty")

#     ats_col = _pick_column(df.columns, _ATS_HEADERS, 'ATS ID')
#     process_col = _pick_column(df.columns, _PROCESS_HEADERS, 'Process')
#     location_col = _pick_column(df.columns, _LOCATION_HEADERS, 'Location')

#     df = df[[ats_col, process_col, location_col]].copy()
#     df.columns = ['ats_id', 'process', 'location']

#     # Blank cells (NaN, empty string, the literal "nan" pandas leaves behind)
#     # carry no mapping, so the row is dropped rather than indexed as a real one.
#     for col in ('ats_id', 'process', 'location'):
#         df[col] = df[col].astype(str).str.strip()
#         df.loc[df[col].str.lower().isin(('', 'nan', 'none', 'null')), col] = pd.NA
#     df = df.dropna(subset=['ats_id', 'process', 'location'])

#     df['ats_id'] = df['ats_id'].map(normalise_ats_id)

#     # ATS ID -> (process, location), spelled as the workbook spells them.
#     # Later rows win, which only matters if the workbook ever gains duplicates.
#     index = {
#         row.ats_id: (row.process, row.location)
#         for row in df.itertuples(index=False)
#     }

#     processes = sorted(dict.fromkeys(df['process'].tolist()), key=str.casefold)
#     locations = sorted(dict.fromkeys(df['location'].tolist()), key=str.casefold)

#     # Process -> the locations that process actually runs at. Most processes run
#     # at exactly one site, which is what lets the upload page fill Location in
#     # by itself once a Process is picked. Keyed on the normalised process name
#     # so a lookup is not defeated by casing.
#     process_locations = {}
#     for process, location in zip(df['process'], df['location']):
#         bucket = process_locations.setdefault(_norm_value(process), [])
#         if location not in bucket:
#             bucket.append(location)
#     for bucket in process_locations.values():
#         bucket.sort(key=str.casefold)

#     logger.info(
#         "mapping file loaded: %s ATS IDs, %s processes, %s locations "
#         "(columns: %s / %s / %s)",
#         len(index), len(processes), len(locations),
#         ats_col, process_col, location_col,
#     )

#     return {
#         'index': index,
#         'processes': processes,
#         'locations': locations,
#         'process_locations': process_locations,
#     }


# def get_mapping(force_reload=False):
#     """Return the cached mapping, re-reading the workbook when it changed."""
#     global _cache, _cache_stamp

#     if not os.path.exists(MAPPING_FILE):
#         raise MappingUnavailable("mapping file not found at %s" % MAPPING_FILE)

#     stamp = _file_stamp(MAPPING_FILE)

#     with _lock:
#         if force_reload or _cache is None or _cache_stamp != stamp:
#             _cache = _load()
#             _cache_stamp = stamp
#         return _cache


# def get_processes():
#     """Unique process names for the upload dropdown, blanks removed."""
#     try:
#         return list(get_mapping()['processes'])
#     except MappingUnavailable as exc:
#         logger.error("Process dropdown could not be populated: %s", exc)
#         return []


# def get_locations():
#     """Unique location names for the upload dropdown, blanks removed."""
#     try:
#         return list(get_mapping()['locations'])
#     except MappingUnavailable as exc:
#         logger.error("Location dropdown could not be populated: %s", exc)
#         return []


# def locations_for_process(process):
#     """Locations the given process runs at, spelled as the workbook spells them.

#     Returns an empty list for an unknown process, which callers treat as "no
#     restriction to apply" rather than as "no valid location".
#     """
#     if not process:
#         return []
#     try:
#         return list(get_mapping()['process_locations'].get(_norm_value(process), []))
#     except MappingUnavailable as exc:
#         logger.error("Locations for process %r are unavailable: %s", process, exc)
#         return []


# def get_process_location_map():
#     """{process name: [locations]} for every process, for the upload page.

#     Keyed on the workbook's own spelling of the process so the value can be
#     matched straight against the Process dropdown.
#     """
#     try:
#         data = get_mapping()
#     except MappingUnavailable as exc:
#         logger.error("Process/Location map could not be built: %s", exc)
#         return {}

#     return {
#         process: list(data['process_locations'].get(_norm_value(process), []))
#         for process in data['processes']
#     }


# def find_ats_column(columns):
#     """Name of the ATS ID column among `columns`, or None.

#     The downtime upload format keeps ATS IDs in EmpCode; the wider candidate
#     list covers a file exported with a differently labelled column.
#     """
#     for column in columns:
#         if _norm_header(column) in _ATS_HEADERS:
#             return column
#     return None


# def lookup(ats_id):
#     """(process, location) for one ATS ID, or None when it is not mapped."""
#     return get_mapping()['index'].get(normalise_ats_id(ats_id))


# def validate_ats_ids(ats_ids, selected_process, selected_location):
#     """Check every ATS ID in an upload against the selected Process/Location.

#     Returns (is_valid, error_message). The check is all-or-nothing: a single
#     unmapped or cross-process/location ATS ID fails the whole file, so no
#     partial upload is ever created.

#     Every offending ATS ID is logged with its selected and actual values, so
#     the reason for a rejection can be traced without re-running the upload.
#     """
#     mapping = get_mapping()['index']

#     sel_process_norm = _norm_value(selected_process)
#     sel_location_norm = _norm_value(selected_location)

#     unknown = []     # ATS IDs absent from the mapping file
#     mismatched = []  # (ats_id, actual_process, actual_location)

#     # De-duplicated but order-preserving: one report line per distinct ATS ID.
#     for ats_id in dict.fromkeys(normalise_ats_id(a) for a in ats_ids):
#         if not ats_id or ats_id in ('NAN', 'NONE', 'NULL'):
#             continue

#         mapped = mapping.get(ats_id)
#         if mapped is None:
#             unknown.append(ats_id)
#             continue

#         actual_process, actual_location = mapped
#         if (_norm_value(actual_process) != sel_process_norm
#                 or _norm_value(actual_location) != sel_location_norm):
#             mismatched.append((ats_id, actual_process, actual_location))

#     if unknown:
#         for ats_id in unknown:
#             logger.warning(
#                 "Upload rejected - ATS ID not present in the mapping file | "
#                 "ATS ID: %s | Selected Process: %s | Selected Location: %s",
#                 ats_id, selected_process, selected_location,
#             )
#         logger.warning(
#             "Upload rejected - %s unmapped ATS ID(s): %s",
#             len(unknown), ', '.join(unknown),
#         )
#         # The IDs are named in the message, not just the log: without them the
#         # uploader has no way to tell which row of the file to correct.
#         return False, "%s Inactive ATSid: %s" % (
#             UNKNOWN_ATS_ERROR, ', '.join(unknown),
#         )

#     if mismatched:
#         for ats_id, actual_process, actual_location in mismatched:
#             logger.warning(
#                 "Upload rejected - process/location mismatch | ATS ID: %s | "
#                 "Selected Process: %s | Actual Process: %s | "
#                 "Selected Location: %s | Actual Location: %s",
#                 ats_id, selected_process, actual_process,
#                 selected_location, actual_location,
#             )
#         logger.warning(
#             "Upload rejected - %s ATS ID(s) belong to a different "
#             "process/location: %s",
#             len(mismatched), ', '.join(a for a, _, _ in mismatched),
#         )
#         return False, MISMATCH_ERROR

#     return True, ""
"""Process / Location mapping read from media/mapping/mapping.csv.

The mapping file is the single source of truth for which ATS ID belongs to
which Process and Location. Nothing here is hard-coded: the dropdown values on
the upload page and the validation applied to an uploaded downtime file both
come from this file, so adding a process or moving a site is a change to the
mapping file alone.

The file is read at most once per modification: the parsed result is cached and
the cache is dropped as soon as the file's size or mtime changes, which keeps
an upload request from re-parsing ~5k rows every time.
"""

import logging
import os
import threading

import pandas as pd
from django.conf import settings

logger = logging.getLogger(__name__)

# Location of the mapping file. Overridable so a deployment can point elsewhere
# without a code change.
MAPPING_FILE = os.environ.get(
    'MAPPING_FILE', os.path.join(settings.MEDIA_ROOT, 'mapping', 'mapping.csv')
)

# Header names accepted for each logical column, lower-cased and stripped of
# spaces/underscores before comparison. The workbook currently ships
# "EmpCode / Process / Location", but a re-export with slightly different
# labels must not break the upload page.
_ATS_HEADERS = (
    'empcode', 'empcodes', 'empid', 'employeecode', 'employeeid',
    'atsid', 'atsids', 'atscode', 'atsempcode', 'agentid',
)
_PROCESS_HEADERS = (
    'process', 'processname', 'processes', 'campaign', 'campaignname',
    'lob', 'lobname',
)
_LOCATION_HEADERS = (
    'location', 'locationname', 'locations', 'site', 'sitename',
    'city', 'branch', 'center', 'centre',
)

# Exact wording required by the business for a cross process/location upload.
MISMATCH_ERROR = (
"Upload file contains data for a different process or location.Please correct the file and re-upload it."
)
UNKNOWN_ATS_ERROR = "This ATSid is not active. Please verify and reupload."

_cache = None          # dict returned by _load()
_cache_stamp = None    # (size, mtime) of the workbook the cache was built from
_lock = threading.Lock()


class MappingUnavailable(Exception):
    """Raised when the mapping file is missing or unusable.

    Uploads are blocked rather than waved through: without the mapping there is
    no way to tell whether the file belongs to the selected process/location.
    """


def _norm_header(name):
    """Reduce a column header to a comparable token."""
    return ''.join(str(name).split()).replace('_', '').replace('-', '').lower()


def _norm_value(value):
    """Case- and whitespace-insensitive form used for comparisons only.

    Locations in the workbook are inconsistently cased ("Noida" vs
    "CHANDIGARH"), so comparisons normalise while the value shown in the
    dropdown stays exactly as the workbook spells it.
    """
    return ' '.join(str(value).split()).lower()


def normalise_ats_id(value):
    """Canonical form of an ATS ID: trimmed, upper-cased, no inner spaces."""
    return ''.join(str(value).split()).upper()


def _pick_column(columns, candidates, label):
    """Find the workbook column matching one of `candidates`.

    Falls back to a substring match so a header such as "Process Name (LOB)"
    is still recognised.
    """
    normalised = {_norm_header(col): col for col in columns}

    for candidate in candidates:
        if candidate in normalised:
            return normalised[candidate]

    for norm, original in normalised.items():
        if any(candidate in norm for candidate in candidates):
            return original

    raise MappingUnavailable(
        "mapping file has no recognisable {} column. Columns found: {}".format(
            label, list(columns)
        )
    )


def _file_stamp(path):
    stat = os.stat(path)
    return (stat.st_size, stat.st_mtime_ns)


def _read_mapping_frame(path):
    """Read the mapping file, picking the reader from its extension.

    The file is a CSV today and was a workbook before; the MAPPING_FILE override
    may point at either, so the format is not assumed.
    """
    if os.path.splitext(path)[1].lower() == '.csv':
        return pd.read_csv(path)
    return pd.read_excel(path)


def _load():
    """Parse the mapping file into the cached structure. Caller holds the lock."""
    if not os.path.exists(MAPPING_FILE):
        raise MappingUnavailable("mapping file not found at %s" % MAPPING_FILE)

    try:
        df = _read_mapping_frame(MAPPING_FILE)
    except Exception as exc:  # unreadable / corrupt mapping file
        raise MappingUnavailable(
            "mapping file could not be read: %s" % exc
        ) from exc

    if df.empty:
        raise MappingUnavailable("mapping file is empty")

    ats_col = _pick_column(df.columns, _ATS_HEADERS, 'ATS ID')
    process_col = _pick_column(df.columns, _PROCESS_HEADERS, 'Process')
    location_col = _pick_column(df.columns, _LOCATION_HEADERS, 'Location')

    df = df[[ats_col, process_col, location_col]].copy()
    df.columns = ['ats_id', 'process', 'location']

    # Blank cells (NaN, empty string, the literal "nan" pandas leaves behind)
    # carry no mapping, so the row is dropped rather than indexed as a real one.
    for col in ('ats_id', 'process', 'location'):
        df[col] = df[col].astype(str).str.strip()
        df.loc[df[col].str.lower().isin(('', 'nan', 'none', 'null')), col] = pd.NA
    df = df.dropna(subset=['ats_id', 'process', 'location'])

    df['ats_id'] = df['ats_id'].map(normalise_ats_id)

    # ATS ID -> (process, location), spelled as the workbook spells them.
    # Later rows win, which only matters if the workbook ever gains duplicates.
    index = {
        row.ats_id: (row.process, row.location)
        for row in df.itertuples(index=False)
    }

    processes = sorted(dict.fromkeys(df['process'].tolist()), key=str.casefold)
    locations = sorted(dict.fromkeys(df['location'].tolist()), key=str.casefold)

    # Process -> the locations that process actually runs at. Most processes run
    # at exactly one site, which is what lets the upload page fill Location in
    # by itself once a Process is picked. Keyed on the normalised process name
    # so a lookup is not defeated by casing.
    process_locations = {}
    for process, location in zip(df['process'], df['location']):
        bucket = process_locations.setdefault(_norm_value(process), [])
        if location not in bucket:
            bucket.append(location)
    for bucket in process_locations.values():
        bucket.sort(key=str.casefold)

    logger.info(
        "mapping file loaded: %s ATS IDs, %s processes, %s locations "
        "(columns: %s / %s / %s)",
        len(index), len(processes), len(locations),
        ats_col, process_col, location_col,
    )

    return {
        'index': index,
        'processes': processes,
        'locations': locations,
        'process_locations': process_locations,
    }


def get_mapping(force_reload=False):
    """Return the cached mapping, re-reading the workbook when it changed."""
    global _cache, _cache_stamp

    if not os.path.exists(MAPPING_FILE):
        raise MappingUnavailable("mapping file not found at %s" % MAPPING_FILE)

    stamp = _file_stamp(MAPPING_FILE)

    with _lock:
        if force_reload or _cache is None or _cache_stamp != stamp:
            _cache = _load()
            _cache_stamp = stamp
        return _cache


# Only these processes may be uploaded against. Everything else in
# mapping.xlsx stays in the index - so ATS IDs still resolve and existing files
# keep validating - but is not offered on the upload page.
#
# Matched case- and space-insensitively against the mapping's own spelling, so
# "Emaar" here finds "EMAAR" in the file. A name that is not in the mapping on a
# given day simply does not appear, and starts appearing by itself once the
# daily HRMS dump carries it.
ALLOWED_PROCESSES = (
    'Think Gas',
    'JVVNL',
    'TPDDL',
    'UGVCL',
    'Jindal Steel LTD',
    'E-Saras',
    'Emaar',
    'DIC',
    'RBI Chandigarh',
    'CESC Kolkata',
    'CESC Rajasthan',
    'RBI KOCHI',
    'RBI Bhubaneswar',
    'Bajaj Allianz-Pune',
    'NetMeds',
    'Tamil Nadu CM Helpline',
)

_ALLOWED_PROCESS_KEYS = frozenset(_norm_value(name) for name in ALLOWED_PROCESSES)


def is_allowed_process(name):
    """True when a process may be chosen on the upload page."""
    return _norm_value(name) in _ALLOWED_PROCESS_KEYS


def get_processes():
    """Process names offered on the upload dropdown, blanks removed.

    Restricted to ALLOWED_PROCESSES; the mapping's own spelling is what is
    returned, so the value posted back matches the file exactly.
    """
    try:
        return [p for p in get_mapping()['processes'] if is_allowed_process(p)]
    except MappingUnavailable as exc:
        logger.error("Process dropdown could not be populated: %s", exc)
        return []


def get_locations():
    """Unique location names for the upload dropdown, blanks removed."""
    try:
        return list(get_mapping()['locations'])
    except MappingUnavailable as exc:
        logger.error("Location dropdown could not be populated: %s", exc)
        return []


def locations_for_process(process):
    """Locations the given process runs at, spelled as the workbook spells them.

    Returns an empty list for an unknown process, which callers treat as "no
    restriction to apply" rather than as "no valid location".
    """
    if not process:
        return []
    try:
        return list(get_mapping()['process_locations'].get(_norm_value(process), []))
    except MappingUnavailable as exc:
        logger.error("Locations for process %r are unavailable: %s", process, exc)
        return []


def get_process_location_map():
    """{process name: [locations]} for every process, for the upload page.

    Keyed on the workbook's own spelling of the process so the value can be
    matched straight against the Process dropdown.
    """
    try:
        data = get_mapping()
    except MappingUnavailable as exc:
        logger.error("Process/Location map could not be built: %s", exc)
        return {}

    return {
        process: list(data['process_locations'].get(_norm_value(process), []))
        for process in data['processes']
        if is_allowed_process(process)
    }


def find_ats_column(columns):
    """Name of the ATS ID column among `columns`, or None.

    The downtime upload format keeps ATS IDs in EmpCode; the wider candidate
    list covers a file exported with a differently labelled column.
    """
    for column in columns:
        if _norm_header(column) in _ATS_HEADERS:
            return column
    return None


def lookup(ats_id):
    """(process, location) for one ATS ID, or None when it is not mapped."""
    return get_mapping()['index'].get(normalise_ats_id(ats_id))


def validate_ats_ids(ats_ids, selected_process, selected_location):
    """Check every ATS ID in an upload against the selected Process/Location.

    Returns (is_valid, error_message). The check is all-or-nothing: a single
    unmapped or cross-process/location ATS ID fails the whole file, so no
    partial upload is ever created.

    Every offending ATS ID is logged with its selected and actual values, so
    the reason for a rejection can be traced without re-running the upload.
    """
    mapping = get_mapping()['index']

    sel_process_norm = _norm_value(selected_process)
    sel_location_norm = _norm_value(selected_location)

    unknown = []     # ATS IDs absent from the mapping file
    mismatched = []  # (ats_id, actual_process, actual_location)

    # De-duplicated but order-preserving: one report line per distinct ATS ID.
    for ats_id in dict.fromkeys(normalise_ats_id(a) for a in ats_ids):
        if not ats_id or ats_id in ('NAN', 'NONE', 'NULL'):
            continue

        mapped = mapping.get(ats_id)
        if mapped is None:
            unknown.append(ats_id)
            continue

        actual_process, actual_location = mapped
        if (_norm_value(actual_process) != sel_process_norm
                or _norm_value(actual_location) != sel_location_norm):
            mismatched.append((ats_id, actual_process, actual_location))

    if unknown:
        for ats_id in unknown:
            logger.warning(
                "Upload rejected - ATS ID not present in the mapping file | "
                "ATS ID: %s | Selected Process: %s | Selected Location: %s",
                ats_id, selected_process, selected_location,
            )
        logger.warning(
            "Upload rejected - %s unmapped ATS ID(s): %s",
            len(unknown), ', '.join(unknown),
        )
        # The IDs are named in the message, not just the log: without them the
        # uploader has no way to tell which row of the file to correct.
        return False, "%s Inactive ATSid: %s" % (
            UNKNOWN_ATS_ERROR, ', '.join(unknown),
        )

    if mismatched:
        for ats_id, actual_process, actual_location in mismatched:
            logger.warning(
                "Upload rejected - process/location mismatch | ATS ID: %s | "
                "Selected Process: %s | Actual Process: %s | "
                "Selected Location: %s | Actual Location: %s",
                ats_id, selected_process, actual_process,
                selected_location, actual_location,
            )
        logger.warning(
            "Upload rejected - %s ATS ID(s) belong to a different "
            "process/location: %s",
            len(mismatched), ', '.join(a for a, _, _ in mismatched),
        )
        return False, MISMATCH_ERROR

    return True, ""
