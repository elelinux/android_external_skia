'''
Created on May 16, 2011

@author: bungeman
'''
import bench_util
import getopt
import httplib
import itertools
import json
import os
import re
import sys
import urllib
import urllib2
import xml.sax.saxutils

# We throw out any measurement outside this range, and log a warning.
MIN_REASONABLE_TIME = 0
MAX_REASONABLE_TIME = 99999

# Constants for prefixes in output title used in buildbot.
TITLE_PREAMBLE = 'Bench_Performance_for_'
TITLE_PREAMBLE_LENGTH = len(TITLE_PREAMBLE)

def usage():
    """Prints simple usage information."""

    print '-a <url> the url to use for adding bench values to app engine app.'
    print '   Example: "https://skiadash.appspot.com/add_point".'
    print '   If not set, will skip this step.'
    print '-b <bench> the bench to show.'
    print '-c <config> the config to show (GPU, 8888, 565, etc).'
    print '-d <dir> a directory containing bench_r<revision>_<scalar> files.'
    print '-e <file> file containing expected bench values/ranges.'
    print '   Will raise exception if actual bench values are out of range.'
    print '   See bench_expectations.txt for data format and examples.'
    print '-f <revision>[:<revision>] the revisions to use for fitting.'
    print '   Negative <revision> is taken as offset from most recent revision.'
    print '-i <time> the time to ignore (w, c, g, etc).'
    print '   The flag is ignored when -t is set; otherwise we plot all the'
    print '   times except the one specified here.'
    print '-l <title> title to use for the output graph'
    print '-m <representation> representation of bench value.'
    print '   See _ListAlgorithm class in bench_util.py.'
    print '-o <path> path to which to write output.'
    print '-r <revision>[:<revision>] the revisions to show.'
    print '   Negative <revision> is taken as offset from most recent revision.'
    print '-s <setting>[=<value>] a setting to show (alpha, scalar, etc).'
    print '-t <time> the time to show (w, c, g, etc).'
    print '-x <int> the desired width of the svg.'
    print '-y <int> the desired height of the svg.'
    print '--default-setting <setting>[=<value>] setting for those without.'
    

class Label:
    """The information in a label.
    
    (str, str, str, str, {str:str})"""
    def __init__(self, bench, config, time_type, settings):
        self.bench = bench
        self.config = config
        self.time_type = time_type
        self.settings = settings
    
    def __repr__(self):
        return "Label(%s, %s, %s, %s)" % (
                   str(self.bench),
                   str(self.config),
                   str(self.time_type),
                   str(self.settings),
               )
    
    def __str__(self):
        return "%s_%s_%s_%s" % (
                   str(self.bench),
                   str(self.config),
                   str(self.time_type),
                   str(self.settings),
               )
    
    def __eq__(self, other):
        return (self.bench == other.bench and
                self.config == other.config and
                self.time_type == other.time_type and
                self.settings == other.settings)
    
    def __hash__(self):
        return (hash(self.bench) ^
                hash(self.config) ^
                hash(self.time_type) ^
                hash(frozenset(self.settings.iteritems())))

def get_latest_revision(directory):
    """Returns the latest revision number found within this directory.
    """
    latest_revision_found = -1
    for bench_file in os.listdir(directory):
        file_name_match = re.match('bench_r(\d+)_(\S+)', bench_file)
        if (file_name_match is None):
            continue
        revision = int(file_name_match.group(1))
        if revision > latest_revision_found:
            latest_revision_found = revision
    if latest_revision_found < 0:
        return None
    else:
        return latest_revision_found

def parse_dir(directory, default_settings, oldest_revision, newest_revision,
              rep):
    """Parses bench data from files like bench_r<revision>_<scalar>.
    
    (str, {str, str}, Number, Number) -> {int:[BenchDataPoints]}"""
    revision_data_points = {} # {revision : [BenchDataPoints]}
    file_list = os.listdir(directory)
    file_list.sort()
    for bench_file in file_list:
        file_name_match = re.match('bench_r(\d+)_(\S+)', bench_file)
        if (file_name_match is None):
            continue

        revision = int(file_name_match.group(1))
        scalar_type = file_name_match.group(2)

        if (revision < oldest_revision or revision > newest_revision):
            continue

        file_handle = open(directory + '/' + bench_file, 'r')

        if (revision not in revision_data_points):
            revision_data_points[revision] = []
        default_settings['scalar'] = scalar_type
        revision_data_points[revision].extend(
                        bench_util.parse(default_settings, file_handle, rep))
        file_handle.close()
    return revision_data_points

def add_to_revision_data_points(new_point, revision, revision_data_points):
    """Add new_point to set of revision_data_points we are building up.
    """
    if (revision not in revision_data_points):
        revision_data_points[revision] = []
    revision_data_points[revision].append(new_point)

def filter_data_points(unfiltered_revision_data_points):
    """Filter out any data points that are utterly bogus.

    Returns (allowed_revision_data_points, ignored_revision_data_points):
        allowed_revision_data_points: points that survived the filter
        ignored_revision_data_points: points that did NOT survive the filter
    """
    allowed_revision_data_points = {} # {revision : [BenchDataPoints]}
    ignored_revision_data_points = {} # {revision : [BenchDataPoints]}
    revisions = unfiltered_revision_data_points.keys()
    revisions.sort()
    for revision in revisions:
        for point in unfiltered_revision_data_points[revision]:
            if point.time < MIN_REASONABLE_TIME or point.time > MAX_REASONABLE_TIME:
                add_to_revision_data_points(point, revision, ignored_revision_data_points)
            else:
                add_to_revision_data_points(point, revision, allowed_revision_data_points)
    return (allowed_revision_data_points, ignored_revision_data_points)

def get_abs_path(relative_path):
    """My own implementation of os.path.abspath() that better handles paths
    which approach Window's 260-character limit.
    See https://code.google.com/p/skia/issues/detail?id=674

    This implementation adds path components one at a time, resolving the
    absolute path each time, to take advantage of any chdirs into outer
    directories that will shorten the total path length.

    TODO: share a single implementation with upload_to_bucket.py, instead
    of pasting this same code into both files."""
    if os.path.isabs(relative_path):
        return relative_path
    path_parts = relative_path.split(os.sep)
    abs_path = os.path.abspath('.')
    for path_part in path_parts:
        abs_path = os.path.abspath(os.path.join(abs_path, path_part))
    return abs_path

def redirect_stdout(output_path):
    """Redirect all following stdout to a file.

    You may be asking yourself, why redirect stdout within Python rather than
    redirecting the script's output in the calling shell?
    The answer lies in https://code.google.com/p/skia/issues/detail?id=674
    ('buildbot: windows GenerateBenchGraphs step fails due to filename length'):
    On Windows, we need to generate the absolute path within Python to avoid
    the operating system's 260-character pathname limit, including chdirs."""
    abs_path = get_abs_path(output_path)
    sys.stdout = open(abs_path, 'w')

def create_lines(revision_data_points, settings
               , bench_of_interest, config_of_interest, time_of_interest
               , time_to_ignore):
    """Convert revision data into a dictionary of line data.
    
    Args:
      revision_data_points: a dictionary with integer keys (revision #) and a
          list of bench data points as values
      settings: a dictionary of setting names to value
      bench_of_interest: optional filter parameters: which bench type is of
          interest. If None, process them all.
      config_of_interest: optional filter parameters: which config type is of
          interest. If None, process them all.
      time_of_interest: optional filter parameters: which timer type is of
          interest. If None, process them all.
      time_to_ignore: optional timer type to ignore

    Returns:
      a dictionary of this form:
          keys = Label objects
          values = a list of (x, y) tuples sorted such that x values increase
              monotonically
    """
    revisions = revision_data_points.keys()
    revisions.sort()
    lines = {} # {Label:[(x,y)] | x[n] <= x[n+1]}
    for revision in revisions:
        for point in revision_data_points[revision]:
            if (bench_of_interest is not None and
                not bench_of_interest == point.bench):
                continue
            
            if (config_of_interest is not None and
                not config_of_interest == point.config):
                continue
            
            if (time_of_interest is not None and
                not time_of_interest == point.time_type):
                continue
            elif (time_to_ignore is not None and
                  time_to_ignore == point.time_type):
                continue
            
            skip = False
            for key, value in settings.items():
                if key in point.settings and point.settings[key] != value:
                    skip = True
                    break
            if skip:
                continue
            
            line_name = Label(point.bench
                            , point.config
                            , point.time_type
                            , point.settings)
            
            if line_name not in lines:
                lines[line_name] = []
            
            lines[line_name].append((revision, point.time))
            
    return lines

def bounds(lines):
    """Finds the bounding rectangle for the lines.
    
    {Label:[(x,y)]} -> ((min_x, min_y),(max_x,max_y))"""
    min_x = bench_util.Max
    min_y = bench_util.Max
    max_x = bench_util.Min
    max_y = bench_util.Min
    
    for line in lines.itervalues():
        for x, y in line:
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)
            
    return ((min_x, min_y), (max_x, max_y))

def create_regressions(lines, start_x, end_x):
    """Creates regression data from line segments.
    
    ({Label:[(x,y)] | [n].x <= [n+1].x}, Number, Number)
        -> {Label:LinearRegression}"""
    regressions = {} # {Label : LinearRegression}
    
    for label, line in lines.iteritems():
        regression_line = [p for p in line if start_x <= p[0] <= end_x]
        
        if (len(regression_line) < 2):
            continue
        regression = bench_util.LinearRegression(regression_line)
        regressions[label] = regression
    
    return regressions

def bounds_slope(regressions):
    """Finds the extreme up and down slopes of a set of linear regressions.
    
    ({Label:LinearRegression}) -> (max_up_slope, min_down_slope)"""
    max_up_slope = 0
    min_down_slope = 0
    for regression in regressions.itervalues():
        min_slope = regression.find_min_slope()
        max_up_slope = max(max_up_slope, min_slope)
        min_down_slope = min(min_down_slope, min_slope)
    
    return (max_up_slope, min_down_slope)

def main():
    """Parses command line and writes output."""
    
    try:
        opts, _ = getopt.getopt(sys.argv[1:]
                                 , "a:b:c:d:e:f:i:l:m:o:r:s:t:x:y:"
                                 , "default-setting=")
    except getopt.GetoptError, err:
        print str(err) 
        usage()
        sys.exit(2)
    
    directory = None
    config_of_interest = None
    bench_of_interest = None
    time_of_interest = None
    time_to_ignore = None
    output_path = None
    bench_expectations = {}
    appengine_url = None  # used for adding data to appengine datastore
    rep = None  # bench representation algorithm
    revision_range = '0:'
    regression_range = '0:'
    latest_revision = None
    requested_height = None
    requested_width = None
    title = 'Bench graph'
    settings = {}
    default_settings = {}

    def parse_range(range):
        """Takes '<old>[:<new>]' as a string and returns (old, new).
        Any revision numbers that are dependent on the latest revision number
        will be filled in based on latest_revision.
        """
        old, _, new = range.partition(":")
        old = int(old)
        if old < 0:
            old += latest_revision;
        if not new:
            new = latest_revision;
        new = int(new)
        if new < 0:
            new += latest_revision;
        return (old, new)

    def add_setting(settings, setting):
        """Takes <key>[=<value>] adds {key:value} or {key:True} to settings."""
        name, _, value = setting.partition('=')
        if not value:
            settings[name] = True
        else:
            settings[name] = value

    def read_expectations(expectations, filename):
        """Reads expectations data from file and put in expectations dict."""
        for expectation in open(filename).readlines():
            elements = expectation.strip().split(',')
            if not elements[0] or elements[0].startswith('#'):
                continue
            if len(elements) != 5:
                raise Exception("Invalid expectation line format: %s" %
                                expectation)
            bench_entry = elements[0] + ',' + elements[1]
            if bench_entry in expectations:
                raise Exception("Dup entries for bench expectation %s" %
                                bench_entry)
            # [<Bench_BmpConfig_TimeType>,<Platform-Alg>] -> (LB, UB)
            expectations[bench_entry] = (float(elements[-2]),
                                         float(elements[-1]))

    def check_expectations(lines, expectations, newest_revision, key_suffix):
        """Check if there are benches in latest rev outside expected range.
        For exceptions, also outputs URL link for the dashboard plot.
        The link history token format here only works for single-line plots.
        """
        # The platform for this bot, to pass to the dashboard plot.
        platform = key_suffix[ : key_suffix.rfind('-')]
        # Starting revision for the dashboard plot.
        start_rev = str(newest_revision - 100)  # Displays about 100 revisions.
        exceptions = []
        for line in lines:
            line_str = str(line)
            line_str = line_str[ : line_str.find('_{')]
            bench_platform_key = line_str + ',' + key_suffix
            this_revision, this_bench_value = lines[line][-1]
            if (this_revision != newest_revision or
                bench_platform_key not in expectations):
                # Skip benches without value for latest revision.
                continue
            this_min, this_max = expectations[bench_platform_key]
            if this_bench_value < this_min or this_bench_value > this_max:
                link = ''
                # For skp benches out of range, create dashboard plot link.
                if line_str.find('.skp_') > 0:
                    # Extract bench and config for dashboard plot.
                    bench, config = line_str.strip('_').split('.skp_')
                    link = ' <a href="'
                    link += 'http://go/skpdash/SkpDash.html#%s~%s~%s~%s" ' % (
                        start_rev, bench, platform, config)
                    link += 'target="_blank">graph</a>'
                exception = 'Bench %s value %s out of range [%s, %s].%s' % (
                    bench_platform_key, this_bench_value, this_min, this_max,
                    link)
                exceptions.append(exception)
        if exceptions:
            raise Exception('Bench values out of range:\n' +
                            '\n'.join(exceptions))

    def write_to_appengine(line_data_dict, url, newest_revision, bot):
        """Writes latest bench values to appengine datastore.
          line_data_dict: dictionary from create_lines.
          url: the appengine url used to send bench values to write
          newest_revision: the latest revision that this script reads
          bot: the bot platform the bench is run on
        """
        config_data_dic = {}
        for label in line_data_dict.iterkeys():
            if not label.bench.endswith('.skp') or label.time_type:
                # filter out non-picture and non-walltime benches
                continue
            config = label.config
            rev, val = line_data_dict[label][-1]
            # This assumes that newest_revision is >= the revision of the last
            # data point we have for each line.
            if rev != newest_revision:
                continue
            if config not in config_data_dic:
                config_data_dic[config] = []
            config_data_dic[config].append(label.bench.replace('.skp', '') +
                ':%.2f' % val)
        for config in config_data_dic:
            if config_data_dic[config]:
                data = {'master': 'Skia', 'bot': bot, 'test': config,
                        'revision': newest_revision,
                        'benches': ','.join(config_data_dic[config])}
                req = urllib2.Request(appengine_url,
                    urllib.urlencode({'data': json.dumps(data)}))
                try:
                    urllib2.urlopen(req)
                except urllib2.HTTPError, e:
                    sys.stderr.write("HTTPError for JSON data %s: %s\n" % (
                        data, e))
                except urllib2.URLError, e:
                    sys.stderr.write("URLError for JSON data %s: %s\n" % (
                        data, e))
                except httplib.HTTPException, e:
                    sys.stderr.write("HTTPException for JSON data %s: %s\n" % (
                        data, e))

    try:
        for option, value in opts:
            if option == "-a":
                appengine_url = value
            elif option == "-b":
                bench_of_interest = value
            elif option == "-c":
                config_of_interest = value
            elif option == "-d":
                directory = value
            elif option == "-e":
                read_expectations(bench_expectations, value)
            elif option == "-f":
                regression_range = value
            elif option == "-i":
                time_to_ignore = value
            elif option == "-l":
                title = value
            elif option == "-m":
                rep = value
            elif option == "-o":
                output_path = value
                redirect_stdout(output_path)
            elif option == "-r":
                revision_range = value
            elif option == "-s":
                add_setting(settings, value)
            elif option == "-t":
                time_of_interest = value
            elif option == "-x":
                requested_width = int(value)
            elif option == "-y":
                requested_height = int(value)
            elif option == "--default-setting":
                add_setting(default_settings, value)
            else:
                usage()
                assert False, "unhandled option"
    except ValueError:
        usage()
        sys.exit(2)

    if directory is None:
        usage()
        sys.exit(2)

    if not output_path:
        print 'Warning: No output path provided. No graphs will be written.'

    if time_of_interest:
        time_to_ignore = None

    # The title flag (-l) provided in buildbot slave is in the format
    # Bench_Performance_for_<platform>, and we want to extract <platform>
    # for use in platform_and_alg to track matching benches later. If title flag
    # is not in this format, there may be no matching benches in the file
    # provided by the expectation_file flag (-e).
    bot = title  # To store the platform as bot name
    platform_and_alg = title
    if platform_and_alg.startswith(TITLE_PREAMBLE):
        bot = platform_and_alg[TITLE_PREAMBLE_LENGTH:]
        platform_and_alg = bot + '-' + rep
    title += ' [representation: %s]' % rep

    latest_revision = get_latest_revision(directory)
    oldest_revision, newest_revision = parse_range(revision_range)
    oldest_regression, newest_regression = parse_range(regression_range)

    unfiltered_revision_data_points = parse_dir(directory
                                   , default_settings
                                   , oldest_revision
                                   , newest_revision
                                   , rep)

    # Filter out any data points that are utterly bogus... make sure to report
    # that we did so later!
    (allowed_revision_data_points, ignored_revision_data_points) = filter_data_points(
        unfiltered_revision_data_points)

    # Update oldest_revision and newest_revision based on the data we could find
    all_revision_numbers = allowed_revision_data_points.keys()
    oldest_revision = min(all_revision_numbers)
    newest_revision = max(all_revision_numbers)

    lines = create_lines(allowed_revision_data_points
                   , settings
                   , bench_of_interest
                   , config_of_interest
                   , time_of_interest
                   , time_to_ignore)

    regressions = create_regressions(lines
                                   , oldest_regression
                                   , newest_regression)

    if output_path:
        output_xhtml(lines, oldest_revision, newest_revision,
                     ignored_revision_data_points, regressions, requested_width,
                     requested_height, title)

    if appengine_url:
        write_to_appengine(lines, appengine_url, newest_revision, bot)

    if bench_expectations:
        check_expectations(lines, bench_expectations, newest_revision,
                           platform_and_alg)

def qa(out):
    """Stringify input and quote as an xml attribute."""
    return xml.sax.saxutils.quoteattr(str(out))
def qe(out):
    """Stringify input and escape as xml data."""
    return xml.sax.saxutils.escape(str(out))

def create_select(qualifier, lines, select_id=None):
    """Output select with options showing lines which qualifier maps to it.
    
    ((Label) -> str, {Label:_}, str?) -> _"""
    options = {} #{ option : [Label]}
    for label in lines.keys():
        option = qualifier(label)
        if (option not in options):
            options[option] = []
        options[option].append(label)
    option_list = list(options.keys())
    option_list.sort()
    print '<select class="lines"',
    if select_id is not None:
        print 'id=%s' % qa(select_id)
    print 'multiple="true" size="10" onchange="updateSvg();">'
    for option in option_list:
        print '<option value=' + qa('[' + 
        reduce(lambda x,y:x+json.dumps(str(y))+',',options[option],"")[0:-1]
        + ']') + '>'+qe(option)+'</option>'
    print '</select>'

def output_ignored_data_points_warning(ignored_revision_data_points):
    """Write description of ignored_revision_data_points to stdout as xhtml.
    """
    num_ignored_points = 0
    description = ''
    revisions = ignored_revision_data_points.keys()
    if revisions:
        revisions.sort()
        revisions.reverse()
        for revision in revisions:
            num_ignored_points += len(ignored_revision_data_points[revision])
            points_at_this_revision = []
            for point in ignored_revision_data_points[revision]:
                points_at_this_revision.append(point.bench)
            points_at_this_revision.sort()
            description += 'r%d: %s\n' % (revision, points_at_this_revision)
    if num_ignored_points == 0:
        print 'Did not discard any data points; all were within the range [%d-%d]' % (
            MIN_REASONABLE_TIME, MAX_REASONABLE_TIME)
    else:
        print '<table width="100%" bgcolor="ff0000"><tr><td align="center">'
        print 'Discarded %d data points outside of range [%d-%d]' % (
            num_ignored_points, MIN_REASONABLE_TIME, MAX_REASONABLE_TIME)
        print '</td></tr><tr><td width="100%" align="center">'
        print ('<textarea rows="4" style="width:97%" readonly="true" wrap="off">'
            + qe(description) + '</textarea>')
        print '</td></tr></table>'

def output_xhtml(lines, oldest_revision, newest_revision, ignored_revision_data_points,
                 regressions, requested_width, requested_height, title):
    """Outputs an svg/xhtml view of the data."""
    print '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN"',
    print '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">'
    print '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">'
    print '<head>'
    print '<title>%s</title>' % qe(title)
    print '</head>'
    print '<body>'
    
    output_svg(lines, regressions, requested_width, requested_height)

    #output the manipulation controls
    print """
<script type="text/javascript">//<![CDATA[
    function getElementsByClass(node, searchClass, tag) {
        var classElements = new Array();
        var elements = node.getElementsByTagName(tag);
        var pattern = new RegExp("^|\\s"+searchClass+"\\s|$");
        for (var i = 0, elementsFound = 0; i < elements.length; ++i) {
            if (pattern.test(elements[i].className)) {
                classElements[elementsFound] = elements[i];
                ++elementsFound;
            }
        }
        return classElements;
    }
    function getAllLines() {
        var selectElem = document.getElementById('benchSelect');
        var linesObj = {};
        for (var i = 0; i < selectElem.options.length; ++i) {
            var lines = JSON.parse(selectElem.options[i].value);
            for (var j = 0; j < lines.length; ++j) {
                linesObj[lines[j]] = true;
            }
        }
        return linesObj;
    }
    function getOptions(selectElem) {
        var linesSelectedObj = {};
        for (var i = 0; i < selectElem.options.length; ++i) {
            if (!selectElem.options[i].selected) continue;
            
            var linesSelected = JSON.parse(selectElem.options[i].value);
            for (var j = 0; j < linesSelected.length; ++j) {
                linesSelectedObj[linesSelected[j]] = true;
            }
        }
        return linesSelectedObj;
    }
    function objectEmpty(obj) {
        for (var p in obj) {
            return false;
        }
        return true;
    }
    function markSelectedLines(selectElem, allLines) {
        var linesSelected = getOptions(selectElem);
        if (!objectEmpty(linesSelected)) {
            for (var line in allLines) {
                allLines[line] &= (linesSelected[line] == true);
            }
        }
    }
    function updateSvg() {
        var allLines = getAllLines();
        
        var selects = getElementsByClass(document, 'lines', 'select');
        for (var i = 0; i < selects.length; ++i) {
            markSelectedLines(selects[i], allLines);
        }
        
        for (var line in allLines) {
            var svgLine = document.getElementById(line);
            var display = (allLines[line] ? 'inline' : 'none');
            svgLine.setAttributeNS(null,'display', display);
        }
    }
    
    function mark(markerId) {
        for (var line in getAllLines()) {
            var svgLineGroup = document.getElementById(line);
            var display = svgLineGroup.getAttributeNS(null,'display');
            if (display == null || display == "" || display != "none") {
                var svgLine = document.getElementById(line+'_line');
                if (markerId == null) {
                    svgLine.removeAttributeNS(null,'marker-mid');
                } else {
                    svgLine.setAttributeNS(null,'marker-mid', markerId);
                }
            }
        }
    }
//]]></script>"""

    all_settings = {}
    variant_settings = set()
    for label in lines.keys():
        for key, value  in label.settings.items():
            if key not in all_settings:
                all_settings[key] = value
            elif all_settings[key] != value:
                variant_settings.add(key)

    print '<table border="0" width="%s">' % requested_width
    #output column headers
    print """
<tr valign="top"><td width="50%">
<table border="0" width="100%">
<tr><td align="center"><table border="0">
<form>
<tr valign="bottom" align="center">
<td width="1">Bench&nbsp;Type</td>
<td width="1">Bitmap Config</td>
<td width="1">Timer&nbsp;Type (Cpu/Gpu/wall)</td>
"""

    for k in variant_settings:
        print '<td width="1">%s</td>' % qe(k)

    print '<td width="1"><!--buttons--></td></tr>'

    #output column contents
    print '<tr valign="top" align="center">'
    print '<td width="1">'
    create_select(lambda l: l.bench, lines, 'benchSelect')
    print '</td><td width="1">'
    create_select(lambda l: l.config, lines)
    print '</td><td width="1">'
    create_select(lambda l: l.time_type, lines)

    for k in variant_settings:
        print '</td><td width="1">'
        create_select(lambda l: l.settings.get(k, " "), lines)

    print '</td><td width="1"><button type="button"',
    print 'onclick=%s' % qa("mark('url(#circleMark)'); return false;"),
    print '>Mark Points</button>'
    print '<button type="button" onclick="mark(null);">Clear Points</button>'
    print '</td>'
    print """
</tr>
</form>
</table></td></tr>
<tr><td align="center">
<hr />
"""

    output_ignored_data_points_warning(ignored_revision_data_points)
    print '</td></tr></table>'
    print '</td><td width="2%"><!--gutter--></td>'

    print '<td><table border="0">'
    print '<tr><td align="center">%s<br></br>revisions r%s - r%s</td></tr>' % (
        qe(title),
        bench_util.CreateRevisionLink(oldest_revision),
        bench_util.CreateRevisionLink(newest_revision))
    print """
<tr><td align="left">
<p>Brighter red indicates tests that have gotten worse; brighter green
indicates tests that have gotten better.</p>
<p>To highlight individual tests, hold down CONTROL and mouse over
graph lines.</p>
<p>To highlight revision numbers, hold down SHIFT and mouse over
the graph area.</p>
<p>To only show certain tests on the graph, select any combination of
tests in the selectors at left.  (To show all, select all.)</p>
<p>Use buttons at left to mark/clear points on the lines for selected
benchmarks.</p>
</td></tr>
</table>

</td>
</tr>
</table>
</body>
</html>"""
    
def compute_size(requested_width, requested_height, rev_width, time_height):
    """Converts potentially empty requested size into a concrete size.
    
    (Number?,  Number?) -> (Number, Number)"""
    pic_width = 0
    pic_height = 0
    if (requested_width is not None and requested_height is not None):
        pic_height = requested_height
        pic_width = requested_width
    
    elif (requested_width is not None):
        pic_width = requested_width
        pic_height = pic_width * (float(time_height) / rev_width)
        
    elif (requested_height is not None):
        pic_height = requested_height
        pic_width = pic_height * (float(rev_width) / time_height)
        
    else:
        pic_height = 800
        pic_width = max(rev_width*3
                      , pic_height * (float(rev_width) / time_height))
    
    return (pic_width, pic_height)

def output_svg(lines, regressions, requested_width, requested_height):
    """Outputs an svg view of the data."""
    
    (global_min_x, _), (global_max_x, global_max_y) = bounds(lines)
    max_up_slope, min_down_slope = bounds_slope(regressions)

    #output
    global_min_y = 0
    x = global_min_x
    y = global_min_y
    w = global_max_x - global_min_x
    h = global_max_y - global_min_y
    font_size = 16
    line_width = 2

    # If there is nothing to see, don't try to draw anything.
    if w == 0 or h == 0:
        return

    pic_width, pic_height = compute_size(requested_width, requested_height
                                       , w, h)
    
    def cw(w1):
        """Converts a revision difference to display width."""
        return (pic_width / float(w)) * w1
    def cx(x):
        """Converts a revision to a horizontal display position."""
        return cw(x - global_min_x)

    def ch(h1):
        """Converts a time difference to a display height."""
        return -(pic_height / float(h)) * h1
    def cy(y):
        """Converts a time to a vertical display position."""
        return pic_height + ch(y - global_min_y)
    
    print '<!--Picture height %.2f corresponds to bench value %.2f.-->' % (
        pic_height, h)
    print '<svg',
    print 'width=%s' % qa(str(pic_width)+'px')
    print 'height=%s' % qa(str(pic_height)+'px')
    print 'viewBox="0 0 %s %s"' % (str(pic_width), str(pic_height))
    print 'onclick=%s' % qa(
            "var event = arguments[0] || window.event;"
            " if (event.shiftKey) { highlightRevision(null); }"
            " if (event.ctrlKey) { highlight(null); }"
            " return false;")
    print 'xmlns="http://www.w3.org/2000/svg"'
    print 'xmlns:xlink="http://www.w3.org/1999/xlink">'
    
    print """
<defs>
    <marker id="circleMark"
      viewBox="0 0 2 2" refX="1" refY="1"
      markerUnits="strokeWidth"
      markerWidth="2" markerHeight="2"
      orient="0">
      <circle cx="1" cy="1" r="1"/>
    </marker>
</defs>"""
    
    #output the revisions
    print """
<script type="text/javascript">//<![CDATA[
    var previousRevision;
    var previousRevisionFill;
    var previousRevisionStroke
    function highlightRevision(id) {
        if (previousRevision == id) return;

        document.getElementById('revision').firstChild.nodeValue = 'r' + id;
        document.getElementById('rev_link').setAttribute('xlink:href',
            'http://code.google.com/p/skia/source/detail?r=' + id);
        
        var preRevision = document.getElementById(previousRevision);
        if (preRevision) {
            preRevision.setAttributeNS(null,'fill', previousRevisionFill);
            preRevision.setAttributeNS(null,'stroke', previousRevisionStroke);
        }
        
        var revision = document.getElementById(id);
        previousRevision = id;
        if (revision) {
            previousRevisionFill = revision.getAttributeNS(null,'fill');
            revision.setAttributeNS(null,'fill','rgb(100%, 95%, 95%)');
            
            previousRevisionStroke = revision.getAttributeNS(null,'stroke');
            revision.setAttributeNS(null,'stroke','rgb(100%, 90%, 90%)');
        }
    }
//]]></script>"""
    
    def print_rect(x, y, w, h, revision):
        """Outputs a revision rectangle in display space,
           taking arguments in revision space."""
        disp_y = cy(y)
        disp_h = ch(h)
        if disp_h < 0:
            disp_y += disp_h
            disp_h = -disp_h
        
        print '<rect id=%s x=%s y=%s' % (qa(revision), qa(cx(x)), qa(disp_y),),
        print 'width=%s height=%s' % (qa(cw(w)), qa(disp_h),),
        print 'fill="white"',
        print 'stroke="rgb(98%%,98%%,88%%)" stroke-width=%s' % qa(line_width),
        print 'onmouseover=%s' % qa(
                "var event = arguments[0] || window.event;"
                " if (event.shiftKey) {"
                    " highlightRevision('"+str(revision)+"');"
                    " return false;"
                " }"),
        print ' />'
    
    xes = set()
    for line in lines.itervalues():
        for point in line:
            xes.add(point[0])
    revisions = list(xes)
    revisions.sort()
    
    left = x
    current_revision = revisions[0]
    for next_revision in revisions[1:]:
        width = (((next_revision - current_revision) / 2.0)
                 + (current_revision - left))
        print_rect(left, y, width, h, current_revision)
        left += width
        current_revision = next_revision
    print_rect(left, y, x+w - left, h, current_revision)

    #output the lines
    print """
<script type="text/javascript">//<![CDATA[
    var previous;
    var previousColor;
    var previousOpacity;
    function highlight(id) {
        if (previous == id) return;

        document.getElementById('label').firstChild.nodeValue = id;

        var preGroup = document.getElementById(previous);
        if (preGroup) {
            var preLine = document.getElementById(previous+'_line');
            preLine.setAttributeNS(null,'stroke', previousColor);
            preLine.setAttributeNS(null,'opacity', previousOpacity);

            var preSlope = document.getElementById(previous+'_linear');
            if (preSlope) {
                preSlope.setAttributeNS(null,'visibility', 'hidden');
            }
        }

        var group = document.getElementById(id);
        previous = id;
        if (group) {
            group.parentNode.appendChild(group);
            
            var line = document.getElementById(id+'_line');
            previousColor = line.getAttributeNS(null,'stroke');
            previousOpacity = line.getAttributeNS(null,'opacity');
            line.setAttributeNS(null,'stroke', 'blue');
            line.setAttributeNS(null,'opacity', '1');
            
            var slope = document.getElementById(id+'_linear');
            if (slope) {
                slope.setAttributeNS(null,'visibility', 'visible');
            }
        }
    }
//]]></script>"""

    # Add a new element to each item in the 'lines' list: the label in string
    # form.  Then use that element to sort the list.
    sorted_lines = []
    for label, line in lines.items():
        sorted_lines.append([str(label), label, line])
    sorted_lines.sort()

    for label_as_string, label, line in sorted_lines:
        print '<g id=%s>' % qa(label_as_string)
        r = 128
        g = 128
        b = 128
        a = .10
        if label in regressions:
            regression = regressions[label]
            min_slope = regression.find_min_slope()
            if min_slope < 0:
                d = max(0, (min_slope / min_down_slope))
                g += int(d*128)
                a += d*0.9
            elif min_slope > 0:
                d = max(0, (min_slope / max_up_slope))
                r += int(d*128)
                a += d*0.9
            
            slope = regression.slope
            intercept = regression.intercept
            min_x = regression.min_x
            max_x = regression.max_x
            print '<polyline id=%s' % qa(str(label)+'_linear'),
            print 'fill="none" stroke="yellow"',
            print 'stroke-width=%s' % qa(abs(ch(regression.serror*2))),
            print 'opacity="0.5" pointer-events="none" visibility="hidden"',
            print 'points="',
            print '%s,%s' % (str(cx(min_x)), str(cy(slope*min_x + intercept))),
            print '%s,%s' % (str(cx(max_x)), str(cy(slope*max_x + intercept))),
            print '"/>'
        
        print '<polyline id=%s' % qa(str(label)+'_line'),
        print 'onmouseover=%s' % qa(
                "var event = arguments[0] || window.event;"
                " if (event.ctrlKey) {"
                    " highlight('"+str(label).replace("'", "\\'")+"');"
                    " return false;"
                " }"),
        print 'fill="none" stroke="rgb(%s,%s,%s)"' % (str(r), str(g), str(b)),
        print 'stroke-width=%s' % qa(line_width),
        print 'opacity=%s' % qa(a),
        print 'points="',
        for point in line:
            print '%s,%s' % (str(cx(point[0])), str(cy(point[1]))),
        print '"/>'

        print '</g>'

    #output the labels
    print '<text id="label" x="0" y=%s' % qa(font_size),
    print 'font-size=%s> </text>' % qa(font_size)

    print '<a id="rev_link" xlink:href="" target="_top">'
    print '<text id="revision" x="0" y=%s style="' % qa(font_size*2)
    print 'font-size: %s; ' % qe(font_size)
    print 'stroke: #0000dd; text-decoration: underline; '
    print '"> </text></a>'

    print '</svg>'

if __name__ == "__main__":
    main()
