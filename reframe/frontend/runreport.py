# Copyright 2016-2021 Swiss National Supercomputing Centre (CSCS/ETH Zurich)
# ReFrame Project Developers. See the top-level LICENSE file for details.
#
# SPDX-License-Identifier: BSD-3-Clause

import decimal
import json
import jsonschema
import lxml.etree as etree
import os
import re

import reframe as rfm
import reframe.core.exceptions as errors
import reframe.utility.jsonext as jsonext
import reframe.utility.versioning as versioning

DATA_VERSION = '1.3.0'
_SCHEMA = os.path.join(rfm.INSTALL_PREFIX, 'reframe/schemas/runreport.json')


class _RunReport:
    '''A wrapper to the run report providing some additional functionality'''

    def __init__(self, report):
        self._report = report

        # Index all runs by test case; if a test case has run multiple times,
        # only the last time will be indexed
        self._cases_index = {}
        for run in self._report['runs']:
            for tc in run['testcases']:
                c, p, e = tc['name'], tc['system'], tc['environment']
                self._cases_index[c, p, e] = tc

        # Index also the restored cases
        for tc in self._report['restored_cases']:
            c, p, e = tc['name'], tc['system'], tc['environment']
            self._cases_index[c, p, e] = tc

    def __getitem__(self, key):
        return self._report[key]

    def __getattr__(self, name):
        return getattr(self._report, name)

    def slice(self, prop, when=None, unique=False):
        '''Slice the report on property ``prop``.'''

        if unique:
            returned = set()

        for tc in self._report['runs'][-1]['testcases']:
            val = tc[prop]
            if unique and val in returned:
                continue

            if when is None:
                if unique:
                    returned.add(val)

                yield val
            elif tc[when[0]] == when[1]:
                if unique:
                    returned.add(val)

                yield val

    def case(self, check, part, env):
        c, p, e = check.name, part.fullname, env.name
        return self._cases_index.get((c, p, e))

    def restore_dangling(self, graph):
        '''Restore dangling dependencies in graph from the report data.

        Returns the updated graph.
        '''

        restored = []
        for tc, deps in graph.items():
            for d in deps:
                if d not in graph:
                    restored.append(d)
                    self._do_restore(d)

        return graph, restored

    def _do_restore(self, testcase):
        tc = self.case(*testcase)
        if tc is None:
            raise errors.ReframeError(
                f'could not restore testcase {testcase!r}: '
                f'not found in the report file'
            )

        dump_file = os.path.join(tc['stagedir'], '.rfm_testcase.json')
        try:
            with open(dump_file) as fp:
                testcase._check = jsonext.load(fp)
        except (OSError, json.JSONDecodeError) as e:
            raise errors.ReframeError(
                f'could not restore testcase {testcase!r}') from e


def next_report_filename(filepatt, new=True):
    if '{sessionid}' not in filepatt:
        return filepatt

    search_patt = os.path.basename(filepatt).replace('{sessionid}', r'(\d+)')
    new_id = -1
    basedir = os.path.dirname(filepatt) or '.'
    for filename in os.listdir(basedir):
        match = re.match(search_patt, filename)
        if match:
            found_id = int(match.group(1))
            new_id = max(found_id, new_id)

    if new:
        new_id += 1

    return filepatt.format(sessionid=new_id)


def load_report(filename):
    try:
        with open(filename) as fp:
            report = json.load(fp)
    except OSError as e:
        raise errors.ReframeError(
            f'failed to load report file {filename!r}') from e
    except json.JSONDecodeError as e:
        raise errors.ReframeError(
            f'report file {filename!r} is not a valid JSON file') from e

    # Validate the report
    with open(_SCHEMA) as fp:
        schema = json.load(fp)

    try:
        jsonschema.validate(report, schema)
    except jsonschema.ValidationError as e:
        raise errors.ReframeError(f'invalid report {filename!r}') from e

    # Check if the report data is compatible
    found_ver = versioning.parse(
        report['session_info']['data_version']
    )
    required_ver = versioning.parse(DATA_VERSION)
    if found_ver.major != required_ver.major or found_ver < required_ver:
        raise errors.ReframeError(
            f'incompatible report data versions: '
            f'found {found_ver}, required >= {required_ver}'
        )

    return _RunReport(report)


def junit_xml_report(json_report):
    '''Generate a JUnit report from a standard ReFrame JSON report.'''

    xml_testsuites = etree.Element('testsuites')
    xml_testsuite = etree.SubElement(
        xml_testsuites, 'testsuite',
        attrib={
            'errors': '0',
            'failures': str(json_report['session_info']['num_failures']),
            'hostname': json_report['session_info']['hostname'],
            'id': '0',
            'name': 'reframe',
            'package': 'reframe',
            'tests': str(json_report['session_info']['num_cases']),
            'time': str(json_report['session_info']['time_elapsed']),
            'timestamp': json_report['session_info']['time_start'][:-5],
        }
    )
    testsuite_properties = etree.SubElement(xml_testsuite, 'properties')
    # etree.SubElement(testsuite_properties, "property",
    #                  {'name': 'x', 'value': '0'})
    for testid in range(len(json_report['runs'][0]['testcases'])):
        tid = json_report['runs'][0]['testcases'][testid]
        casename = (
            f"{tid['name']}[{tid['system']}, {tid['environment']}]"
        )
        testcase = etree.SubElement(
            xml_testsuite, 'testcase',
            attrib={
                'classname': tid['filename'],
                'name': casename,
                'time': str(decimal.Decimal(tid['time_total'])),
            }
        )
        if tid['result'] == 'failure':
            testcase_msg = etree.SubElement(
                testcase, 'failure', attrib={'type': 'failure',
                                             'message': tid['fail_phase']}
            )
            testcase_msg.text = f"{tid['fail_phase']}: {tid['fail_reason']}"

    testsuite_stdout = etree.SubElement(xml_testsuite, 'system-out')
    testsuite_stdout.text = ''
    testsuite_stderr = etree.SubElement(xml_testsuite, 'system-err')
    testsuite_stderr.text = ''

    # ---
    # testcase_error = etree.SubElement(
    #     xml_testsuite, 'testcase',
    #     attrib={'classname': 'rfmE', 'name': 'test error', 'time': '0'}
    # )
    # testcase_error_msg = etree.SubElement(
    #     testcase_error, 'error',
    #     attrib={'message': 'no test error', 'type': 'error'}
    # )
    # testcase_error_msg.text = 'E'
    # ---
    # testcase_skip = etree.SubElement(
    #     xml_testsuite, 'testcase',
    #     attrib={'classname': 'rfmS', 'name': 'test skip', 'time': '0'}
    # )
    # testcase_skip_msg = etree.SubElement(
    #     testcase_skip, 'skipped',
    #     attrib={'message': 'no test skipped', 'type': 'skipped'}
    # )
    # testcase_skip_msg.text = 'S'

    return xml_testsuites


def junit_dump(xml, fp):
    fp.write(
        etree.tostring(xml, encoding='utf8', pretty_print=True,
                       method='xml', xml_declaration=True).decode()
    )
