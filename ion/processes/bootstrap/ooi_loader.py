#!/usr/bin/env python

"""Parses OOI SAF Instrument Application assets from CSV reports and a mapping spreadsheet."""

__author__ = 'Michael Meisinger'

import csv
import datetime
import os.path
import re
import requests

from pyon.public import log, BadRequest
from ion.core.ooiref import OOIReferenceDesignator
from pyon.datastore.datastore import DatastoreManager, DataStore
from ion.util.geo_utils import GeoUtils
from ion.util.xlsparser import XLSParser

DEFAULT_MAX_DATE = datetime.datetime(2020, 1, 1)

class OOILoader(object):
    def __init__(self, process, container=None, asset_path=None, mapping_path=None):
        self.process = process
        self.container = container or (self.process.container if process else None)
        self.asset_path = asset_path
        self.mapping_path = mapping_path or self.asset_path + "/OOIResourceMappings.xlsx"
        self._extracted = False

    def extract_ooi_assets(self):
        """
        Parses SAF Instrument Application export CSV files into intermediate memory structures.
        This information can later be loaded in to actual load_ion() function.
        """
        if self._extracted:
            return

        if not self.asset_path:
            raise BadRequest("Must provide path for assets: path=dir or assets=dir")
        if self.asset_path.startswith('http'):
            raise BadRequest('Asset path must be local directory, not URL: ' + self.asset_path)

        log.info("Parsing OOI assets from path=%s", self.asset_path)

        categories = [ # Mapping spreadsheet early
                       'NodeTypes',
                       # Core concept attributes
                       'AttributeReportArrays',
                       'AttributeReportClass',
                       'AttributeReportDataProducts',
                       'AttributeReportFamilies',
                       'AttributeReportMakeModel',
                       'AttributeReportNodes',
                       'AttributeReportPorts',
                       'AttributeReportReferenceDesignator',
                       'AttributeReportSeries',
                       'AttributeReportSites',
                       'AttributeReportSubseries',
                       'AttributeReportSubsites',
                       # Additional attributes and links taken from aggregate reports
                       'NodeTypes',
                       'InstrumentCatalogFull',
                       'DataQCLookupTables',
                       'DataProductSpreadsheet',
                       'AllSensorTypeCounts',
                       # Tabs from the mapping spreadsheet
                       'MAP:Arrays',
                       'MAP:Sites',
                       'MAP:Subsites',
                       'MAP:NodeType',
                       'MAP:Nodes',
                       'MAP:Instruments',
                       'MAP:PlatformAgents',
                       'MAP:Series',
                       'MAP:InstAgents',
                       'MAP:DataAgents',
                       'MAP:AgentMap',
                       'MAP:ModelMap',
                       'MAP:DPS',
        ]

        # Holds the object representations of parsed OOI assets by type
        self.ooi_objects = {}
        # Holds a list of attribute names of OOI assets by type
        self.ooi_obj_attrs = {}
        self.warnings = []
        self.csv_files = None

        # Load OOI asset mapping spreadsheet
        if self.mapping_path.startswith('http'):
            contents = requests.get(self.mapping_path).content
            length = len(contents)
            log.info("Loaded mapping spreadsheet from URL %s, size=%s", self.mapping_path, length)
            csv_docs = XLSParser().extract_csvs(contents)
            self.csv_files = csv_docs

        elif self.mapping_path.endswith(".xlsx"):
            # Load from load xlsx file (default OOIResourceMappings.xlsx)
            if os.path.exists(self.mapping_path):
                with open(self.mapping_path, "rb") as f:
                    preload_doc_str = f.read()
                    log.info("Loaded %s mapping file, size=%s", self.mapping_path, len(preload_doc_str))
                    xls_parser = XLSParser()
                    self.csv_files = xls_parser.extract_csvs(preload_doc_str)

        for category in categories:
            row_do = 0

            if category.startswith("MAP:"):
                category = category[4:]
                csv_doc = self.csv_files[category]
                reader = csv.DictReader(csv_doc, delimiter=',')
            else:
                filename = "%s/%s.csv" % (self.asset_path, category)
                #log.debug("Loading category %s from file %s", category, filename)
                try:
                    csvfile = open(filename, "rb")
                    for i in xrange(9):
                        # Skip the first rows, because they are garbage
                        csvfile.readline()
                    csv_doc = csvfile.read()
                    reader = csv.DictReader(csv_doc.splitlines(), delimiter=',')
                except IOError as ioe:
                    log.warn("OOI asset file %s error: %s" % (filename, str(ioe)))

            catfunc = getattr(self, "_parse_%s" % category)
            for row in reader:
                row_do += 1
                catfunc(row)

            log.debug("Loaded assets %s: %d rows read" % (category, row_do))

        # Post processing
        self._post_process()

        # Do some validation checking
        self._perform_ooi_checks()

        if self.warnings:
            log.warn("WARNINGS:\n%s", "\n".join(["%s: %s" % (a, b) for a, b in self.warnings]))

        log.info("Found entries: %s", ", ".join(["%s: %s" % (ot, len(self.ooi_objects[ot])) for ot in sorted(self.ooi_objects.keys())]))

            #import pprint
            #pprint.pprint(oo)
            #log.debug("Type %s has %s attributes", ot, self.ooi_obj_attrs[ot])
            #print ot
            #print "\n".join(sorted(list(self.ooi_obj_attrs[ot])))

        self._extracted = True

    def get_type_assets(self, objtype):
        return self.ooi_objects.get(objtype, {})

    def _add_object_attribute(self, objtype, objid, key, value, value_is_list=False, list_dup_ok=False, change_ok=False, list_sort=True, mapping=None, **kwargs):
        """
        Add a single attribute to an identified object of given type. Create object/type on first occurrence.
        The kwargs are static attributes"""
        if not objid:
            raise Exception("Empty ID")
        ot_objects = self.ooi_objects.setdefault(objtype, {})
        ot_obj_attrs = self.ooi_obj_attrs.setdefault(objtype, set())

        if objid not in ot_objects:
            ot_objects[objid] = dict(id=objid)
        obj_entry = ot_objects[objid]
        if key:
            key = key if mapping is None else mapping.get(key, key)
            if value_is_list:
                if key in obj_entry:
                    if value in obj_entry[key]:
                        if not list_dup_ok:
                            msg = "duplicate_attr_list_value: %s.%s has attribute '%s' with duplicate list value: %s" % (objtype, objid, key, value)
                            self.warnings.append((objid, msg))
                    else:
                        obj_entry[key].append(value)
                        if list_sort:
                            obj_entry[key].sort()
                else:
                    obj_entry[key] = [value]
            elif key in obj_entry and not change_ok:
                msg = "duplicate_attr: %s.%s has duplicate attribute '%s' def: (old=%s, new=%s)" % (objtype, objid, key, obj_entry[key], value)
                self.warnings.append((objid, msg))
            else:
                obj_entry[key] = value
            ot_obj_attrs.add(key)
        for okey, oval in kwargs.iteritems():
            okey = okey if mapping is None else mapping.get(okey, okey)
            if okey in obj_entry and obj_entry[okey] != oval and not change_ok:
                msg = "different_static_attr: %s.%s has different attribute '%s' value: (old=%s, new=%s)" % (objtype, objid, okey, obj_entry[okey], oval)
                self.warnings.append((objid, msg))
            else:
                obj_entry[okey] = oval
            ot_obj_attrs.add(okey)

    # ---- Parse SAF export CSV files ----
    # Note: The following _parse_AttributeReport* function parse decomposed CSV files. Every attribute is in
    # its own row. There are, however, "static" attributes that are repeated with each attribute row.

    def _parse_AttributeReportArrays(self, row):
        ooi_rd = OOIReferenceDesignator(row['Array'])
        if ooi_rd.error or not ooi_rd.rd_type == "asset" or not ooi_rd.rd_subtype == "array":
            msg = "invalid_rd: %s is not an array reference designator" % (ooi_rd.rd)
            self.warnings.append((ooi_rd.rd, msg))
            return
        self._add_object_attribute('array',
            ooi_rd.rd, row['Attribute'], row['AttributeValue'],
            mapping={'Array_Name':'name'},
            Array_Name=row['Array_Name'])

    def _parse_AttributeReportClass(self, row):
        ooi_rd = OOIReferenceDesignator(row['Class'])
        if ooi_rd.error or not ooi_rd.rd_type == "inst_class":
            msg = "invalid_rd: %s is not an instrument class reference designator" % (ooi_rd.rd)
            self.warnings.append((ooi_rd.rd, msg))
            return
        self._add_object_attribute('class',
            ooi_rd.rd, row['Attribute'], row['AttributeValue'],
            mapping={'Description':'description', 'Alternate Instrument Class Name':'alt_name'},
            name=row['Class_Name'])

    def _parse_AttributeReportDataProducts(self, row):
        key = row['Data_Product_Identifier'].strip() + "_L" + row['Data_Product_Level'].strip()
        ooi_rd = OOIReferenceDesignator(key)
        if ooi_rd.error or not ooi_rd.rd_type == "dataproduct" or not ooi_rd.rd_subtype == "level":
            msg = "invalid_rd: %s is not a data product reference designator" % (ooi_rd.rd)
            self.warnings.append((ooi_rd.rd, msg))
            return
        self._add_object_attribute('data_product_type',
            row['Data_Product_Identifier'], row['Attribute'], row['AttributeValue'],
            mapping={'Regime(s)':'regime'},
            Data_Product_Name=row['Data_Product_Name'], Data_Product_Level=row['Data_Product_Level'])

    def _parse_AttributeReportFamilies(self, row):
        self._add_object_attribute('family',
            row['Family'], row['Attribute'], row['AttributeValue'],
            mapping={},
            name=row['Family_Name'])

    def _parse_AttributeReportMakeModel(self, row):
        self._add_object_attribute('makemodel',
            row['Make_Model'], row['Attribute'], row['Attribute_Value'],
            mapping={},
            name=row['Make_Model'],
            Manufacturer=row['Manufacturer'], Make_Model_Description=row['Make_Model_Description'])

    def _parse_AttributeReportNodes(self, row):
        ooi_rd = OOIReferenceDesignator(row['Node'])
        if ooi_rd.error or not ooi_rd.rd_type == "asset" or not ooi_rd.rd_subtype == "node":
            msg = "invalid_rd: %s is not a node designator" % (ooi_rd.rd)
            self.warnings.append((ooi_rd.rd, msg))
            return
        # TODO: Create a default name by structure (subsite name + node type name)
        nodetypes = self.get_type_assets('nodetype')
        if row['Attribute'] == "longitude" and row['AttributeValue']:
            row['AttributeValue'] = str(-1 * float(row['AttributeValue']))
        self._add_object_attribute('node',
            ooi_rd.rd, row['Attribute'], row['AttributeValue'],
            mapping={},
            Node_Type=row['Node_Type'], Node_Site_Sequence=row['Node_Site_Sequence'])

    def _parse_NodeTypes(self, row):
        self._add_object_attribute('nodetype',
                                   row['LNodeType'], None, None,
                                   mapping={'Name':'name'},
                                   Name=row['Name'])

    def _parse_AttributeReportPorts(self, row):
        ooi_rd = OOIReferenceDesignator(row['Port'])
        if ooi_rd.error or not ooi_rd.rd_type == "asset" or not ooi_rd.rd_subtype == "port":
            msg = "invalid_rd: %s is not a port designator" % (ooi_rd.rd)
            self.warnings.append((ooi_rd.rd, msg))
            return
        self._add_object_attribute('port',
            ooi_rd.rd, row['Attribute'], row['AttributeValue'],
            mapping={})

    def _parse_AttributeReportReferenceDesignator(self, row):
        ooi_rd = OOIReferenceDesignator(row['Reference_Designator'])
        if ooi_rd.error or not ooi_rd.rd_type == "asset" or not ooi_rd.rd_subtype == "instrument":
            msg = "invalid_rd: %s is not an instrument designator" % (ooi_rd.rd)
            self.warnings.append((ooi_rd.rd, msg))
            return
        if row['Attribute'] == "longitude" and row['AttributeValue']:
            row['AttributeValue'] = str(-1 * float(row['AttributeValue']))
        self._add_object_attribute('instrument',
            ooi_rd.rd, row['Attribute'], row['AttributeValue'],
            mapping={},
            Class=row['Class'])

    def _parse_AttributeReportSeries(self, row):
        key = row['Class'] + row['Series']
        self._add_object_attribute('series',
            key, row['Attribute'], row['AttributeValue'],
            mapping={'Description':'description'},
            Series=row['Series'], name=row['Series_Name'], Class=row['Class'])

    def _parse_AttributeReportSites(self, row):
        ooi_rd = OOIReferenceDesignator(row['Site'])
        if ooi_rd.error or not ooi_rd.rd_type == "asset" or not ooi_rd.rd_subtype == "site":
            msg = "invalid_rd: %s is not a site designator" % (ooi_rd.rd)
            self.warnings.append((ooi_rd.rd, msg))
            return
        self._add_object_attribute('site',
            ooi_rd.rd, row['Attribute'], row['AttributeValue'],
            name=row['Site_Name'])

    def _parse_AttributeReportSubseries(self, row):
        key = row['Class'] + row['Series'] + row['Subseries']
        self._add_object_attribute('subseries',
            key, row['Attribute'], row['AttributeValue'],
            mapping={'Description':'description'},
            Subseries=row['Subseries'], name=row['Subseries_Name'], Class=row['Class'])

    def _parse_AttributeReportSubsites(self, row):
        ooi_rd = OOIReferenceDesignator(row['Subsite'])
        if ooi_rd.error or not ooi_rd.rd_type == "asset" or not ooi_rd.rd_subtype == "subsite":
            msg = "invalid_rd: %s is not a subsite designator" % (ooi_rd.rd)
            self.warnings.append((ooi_rd.rd, msg))
            return
        if row['Attribute'] == "longitude" and row['AttributeValue']:
            row['AttributeValue'] = str(-1 * float(row['AttributeValue']))
        self._add_object_attribute('subsite',
            ooi_rd.rd, row['Attribute'], row['AttributeValue'],
            name=row['Subsite_Name'])

    def _parse_InstrumentCatalogFull(self, row):
        # This adds the subseries to current sensors and make/model.
        # Also used to infer node types and names
        refid = row['ReferenceDesignator']
        series_id = row['SClass_PublicID']+row['SSeries_PublicID']
        subseries_id = series_id+row['SSubseries_PublicID']
        makemodel = row['MMInstrument_PublicID']
        entry = dict(
            instrument_class=row['SClass_PublicID'],
            instrument_series=row['SSeries_PublicID'],
            instrument_subseries=row['SSubseries_PublicID'],
            instrument_model1=row['SClass_PublicID'],
            instrument_model=series_id,
            makemodel=makemodel,
            ready_for_2013=row['Ready_For_2013_']
        )
        self._add_object_attribute('instrument',
            refid, None, None, **entry)

        if makemodel:
            self._add_object_attribute('class',
                                       row['SClass_PublicID'], 'makemodel', makemodel, value_is_list=True, list_dup_ok=True)
            self._add_object_attribute('series',
                                       series_id, None, None, makemodel=makemodel)
            self._add_object_attribute('subseries',
                subseries_id, None, None, makemodel=makemodel)

        # Build up the node type here
        ntype_txt = row['Textbox11']
        ntype_id = ntype_txt[:2]
        #ntype_name = ntype_txt[3:-1].strip('()')
        #self._add_object_attribute('nodetype',
        #    ntype_id, None, None, name=ntype_name)

        # Determine on which arrays the nodetype is used
        self._add_object_attribute('nodetype',
            ntype_id, 'array_list', refid[:2], value_is_list=True, list_dup_ok=True)

        # Determine on which arrays the instrument class is used
        self._add_object_attribute('class',
            row['SClass_PublicID'], 'array_list', refid[:2], value_is_list=True, list_dup_ok=True)

        self._add_object_attribute('series',
                                   series_id, 'array_list', refid[:2], value_is_list=True, list_dup_ok=True)

    def _parse_DataQCLookupTables(self, row):
        # Adds a list of data products with level to instruments
        refid = row['ReferenceDesignator']
        self._add_object_attribute('instrument',
            refid, None, None, Class=row['SClass_PublicID'])

        dpl = row['Data_Product_With_Level']
        m = re.match('^([A-Z0-9_]{7})\s+\((L\d)\)$', dpl)
        if not m:
            msg = "invalid_rd: %s is not a data product designator" % (dpl)
            self.warnings.append((refid, msg))
            return
        dp_type, dp_level = m.groups()
        dpl = dp_type + "_" + dp_level

        self._add_object_attribute('instrument',
            refid, 'data_product_list', dpl, value_is_list=True)

    def _parse_DataProductSpreadsheet(self, row):
        dp_types = self.ooi_objects['data_product_type']
        dp_type = row['Data_Product_Identifier'].strip()
        dpt_obj = dp_types.get(dp_type, {})
        key = dp_type + "_" + row['Data_Product_Level1'].strip()
        entry = dpt_obj.copy()
        entry.pop("id", None)
        entry.update(dict(
            name=row['Data_Product_Name'].strip(),
            code=dp_type,
            level=row['Data_Product_Level1'].strip(),
            units=row['Units'].strip(),
            dps=row['DPS_DCN_s_'].strip(),
            diagrams=row['Processing_Flow_Diagram_DCN_s_'].strip(),
        ))
        self._add_object_attribute('data_product',
            key, None, None, **entry)
        self._add_object_attribute('data_product',
            key, 'instrument_class_list', row['Instrument_Class'].strip(), value_is_list=True)

    def _parse_AllSensorTypeCounts(self, row):
        # Adds family to instrument class
        self._add_object_attribute('class',
            row['Class'].strip(), 'family', row['Family'].strip())

    # ---- Parse mapping spreadsheet tab ----

    def _parse_Arrays(self, row):
        ooi_rd = row['Reference ID']
        name=row['Name']
        self._add_object_attribute('array',
            ooi_rd, 'name', name, change_ok=True)

    def _parse_Sites(self, row):
        ooi_rd = row['Reference ID']
        name = row['Full Name']
        local_name = row['Name Extension']

        self._add_object_attribute('site',
            ooi_rd, 'name', name, change_ok=True)

        # Aggregated site level entries
        self._add_object_attribute('site',
                                   ooi_rd, 'osite', name)

        self._add_object_attribute('osite',
                                   name, None, None, name=name, local_name=local_name)
        self._add_object_attribute('osite',
                                   name, 'site_rd_list', ooi_rd, value_is_list=True)

    def _parse_Subsites(self, row):
        ooi_rd = row['Reference ID']
        name = row['Full Name']
        local_name = row['Local Name']
        geo_area = row['Site Name']

        coord_dict = dict(
            lat_north = float(row['lat_north']) if row['lat_north'] else None,
            lat_south = float(row['lat_south']) if row['lat_south'] else None,
            lon_east = float(row['lon_east']) if row['lon_east'] else None,
            lon_west = float(row['lon_west']) if row['lon_west'] else None,
            depth_min = float(row['depth_min']) if row['depth_min'] else None,
            depth_max = float(row['depth_max']) if row['depth_max'] else None,
        )
        # Aggregated subsite level entries
        self._add_object_attribute('subsite',
            ooi_rd, 'ssite', name)

        self._add_object_attribute('ssite',
                                   name, None, None, name=name, local_name=local_name, geo_area=geo_area)
        self._add_object_attribute('ssite',
                                   name, 'subsite_rd_list', ooi_rd, value_is_list=True)
        if row['lat_north']:
            self._add_object_attribute('ssite',
                                   name, None, None, **coord_dict)

    def _parse_Nodes(self, row):
        """Asset mappings override for SAF nodes"""
        if row.get('Ignore', None) == "Yes":
            return
        ooi_rd = row['Reference ID']
        name=row['Full Name']
        local_name = row['Name Extension']
        node_entry = dict(
            local_name=local_name,
            parent_id=row['Parent Reference ID'],
            platform_id=row['Platform Reference ID'],
            platform_config_type=row['Platform Configuration Type'],
            platform_agent_type=row['Platform Agent Type'],
            is_platform=row['Platform Reference ID'] == ooi_rd,
            in_saf=row['SAF'] != "No",
            self_port=row['Self Port'],
            uplink_node=row['Uplink Node'],
            uplink_port=row['Uplink Port'],
            deployment_start=row['Start Deployment Cruise'],  # The column type is date. We get it as yyyy-mm-dd
            clone_rd=row.get('Clone', None),
            in_mapping=True,
        )
        if row['Push'] == "Yes" and not row.get('Clone', None):  # Make it break if run with an outdated preload sheet!!
            node_entry["deployment_start"] = "2019-01-01"   # This pushes the node out in deploy date (different from unset)

        self._add_object_attribute('node',
            ooi_rd, None, None, **node_entry)
        self._add_object_attribute('node',
            ooi_rd, 'name', name, change_ok=True)

        node_entry = {}
        if row["lat"] or row["lon"] or row["depth"]:
            #log.debug("Use updated geospatial info from mapping spreadsheet for %s", ooi_rd)
            if row["lat"]:
                node_entry["latitude"] = row["lat"]
            if row["lon"]:
                node_entry["longitude"] = row["lon"]
            if row["depth"]:
                node_entry["depth_subsite"] = row["depth"]
            self._add_object_attribute('node',
                ooi_rd, None, None, change_ok=True, **node_entry)

        # Determine on which arrays the nodetype is used
        self._add_object_attribute('nodetype',
            ooi_rd[9:11], 'array_list', ooi_rd[:2], value_is_list=True, list_dup_ok=True)

    def _parse_NodeType(self, row):
        code = row['Code']
        name = row['Name']
        comp_name = row['Composite Name']
        pa_code = row['PA Code']
        platform_family = row['Platform Family']
        platform_type = row['Platform Type']

        self._add_object_attribute('nodetype',
            code, None, None, name=name, change_ok=True)
        self._add_object_attribute('nodetype',
            code, None, None, pa_code=pa_code, platform_family=platform_family, platform_type=platform_type, comp_name=comp_name)

    def _parse_Instruments(self, row):
        """Asset mappings override for SAF instruments (reference designators)"""
        if row.get('Ignore', None) == "Yes":
            return
        ooi_rd = row['Reference ID']
        entry = dict(
            deployment_start=row['First Deploy Date'],  # Column data type is date. Parsed in yyyy-mm-dd
            clone_rd=row.get('Clone', None),
        )
        if row['Push'] == "Yes":
            entry["deployment_start"] = "2019-02-01"

        self._add_object_attribute('instrument',
            ooi_rd, None, None, **entry)

        entry = {}
        if row["lat"] or row["lon"] or row["depth_min"] or row["depth_max"]:
            if row["lat"]:
                entry["latitude"] = row["lat"]
            if row["lon"]:
                entry["longitude"] = row["lon"]
            if row["depth_min"]:
                entry["depth_port_min"] = row["depth_min"]
            if row["depth_max"]:
                entry["depth_port_max"] = row["depth_max"]
            self._add_object_attribute('instrument',
                ooi_rd, None, None, change_ok=True, **entry)

    def _parse_PlatformAgents(self, row):
        code = row['Code']
        entry = dict(
            name=row['Name'],
            agent_type=row['Agent Type'],
            node_types=row['Node Types'],
            rt_control_path=row['RT Control Path'],
            rt_data_path=row['RT Data Path'],
            rt_data_acquisition=row['RT Data Acquisition'],
            full_data_acquisition=row['Full Data Acquisition'],
            ci_interface_location=row['Marine-CI Interface Location'],
        )
        self._add_object_attribute('platformagent',
            code, None, None, **entry)

    def _parse_Series(self, row):
        code = row['Class Code']
        series = row['Series']
        series_rd = code + series
        ia_code = row['IA Code']
        dart_code = row['DA RT Code']
        dapr_code = row['DA PR Code']
        ia_exists = row['IA'] == "Yes"
        dart_exists = row['DA RT'] == "Yes"
        dapr_exists = row['DA PR'] == "Yes"
        first_avail = row['First Availability']

        if len(series) != 1:
            log.warn("Ignoring asset mappings Series row %s-%s - not a valid code", code, series)
            return
        if series_rd not in self.get_type_assets("series"):
            # This will allow OOI Preload spreadsheet to move ahead of current SAF export
            log.warn("Ignoring asset mappings Series %s-%s - not in current SAF export", code, series)
            return

        entry = dict(
            connection=row['Connection'],
            ia_code=ia_code if ia_exists else "",
            dart_code=dart_code if dart_exists else "",
            dapr_code=dapr_code if dapr_exists else "",
            ia_exists=ia_exists,
            dart_exists=dart_exists,
            dapr_exists=dapr_exists,
            tier1=row['Tier 1'] == "Yes",
            first_avail=self._parse_date(first_avail, DEFAULT_MAX_DATE) if first_avail else DEFAULT_MAX_DATE
            )
        series_objs = self.get_type_assets("series")
        self._add_object_attribute('series',
                                   series_rd, None, None, **entry)
        if ia_exists and ia_code and ia_code != "NA":
            self._add_object_attribute('instagent',
                                       ia_code, None, None,
                                       inst_class=code,
                                       tier1=row['Tier 1'] == "Yes")
            self._add_object_attribute('instagent',
                                       ia_code, 'series_list', series_rd, value_is_list=True, list_dup_ok=True)

        if dart_exists and dart_code and dart_code != "NA":
            self._add_object_attribute('dataagent',
                                       dart_code, None, None,
                                       inst_class=code,
                                       tier1=row['Tier 1'] == "Yes")
            self._add_object_attribute('dataagent',
                                       dart_code, 'series_list', series_rd, value_is_list=True, list_dup_ok=True)

    def _parse_InstAgents(self, row):
        agent_code = row['Agent Code']
        if agent_code:
            self._add_object_attribute('instagent',
                                       agent_code, None, None,
                                       active=row['Active'] == "Yes",
                                       present=row['Present'] == "Yes")

    def _parse_DataAgents(self, row):
        agent_code = row['Agent Code']
        if agent_code:
            self._add_object_attribute('dataagent',
                                       agent_code, None, None,
                                       active=row['Active'] == "Yes",
                                       present=row['Present'] == "Yes")

    def _parse_AgentMap(self, row):
        series = row['Instrument Series']
        node_type = row['Node Type']
        agent_code = row['Agent Code']

        mapping = [agent_code, row['RD Prefix']]

        if series and series in self.get_type_assets("series"):
            self._add_object_attribute('series',
                series, 'agentmap', mapping, value_is_list=True, list_sort=False)

            if agent_code in self.get_type_assets('dataagent'):
                self._add_object_attribute('dataagent',
                                           agent_code, 'series_list', series, value_is_list=True, list_dup_ok=True)
                self._add_object_attribute('dataagent',
                                           agent_code, None, None, inst_class=series[:5])
            if agent_code in self.get_type_assets('instagent'):
                self._add_object_attribute('instagent',
                                           agent_code, 'series_list', series, value_is_list=True, list_dup_ok=True)
                self._add_object_attribute('instagent',
                                           agent_code, None, None, inst_class=series[:5])

        if node_type and node_type in self.get_type_assets("nodetype"):
            self._add_object_attribute('nodetype',
                node_type, 'agentmap', mapping, value_is_list=True, list_sort=False)


    def _parse_ModelMap(self, row):
        series = row['Instrument Series']
        self._add_object_attribute('modelmap',
                                   series, None, None,
                                   primary_series=row['Primary Series'])

    def _parse_DPS(self, row):
        code = row['Code']
        ref_type=row['Ref Type']
        document_name=row['Document Name']
        variant=row['Variant']
        if ref_type and code:
            self._add_object_attribute('datalink',
                                       code, ref_type, row['URL'], value_is_list=True)
            self._add_object_attribute('datalink',
                                       code, ref_type + "_doc", document_name, value_is_list=True)

    # ---- Post-processing and validation ----

    def _perform_ooi_checks(self):
        # Perform some consistency checking on imported objects
        ui_checks = [
            ('ref_exists', ['instrument', 'data_product_list', 'data_product'], None),
            ('ref_exists', ['data_product', 'instrument_class_list', 'class'], None),
            ]
        for check, ckargs, ckkwargs in ui_checks:
            ckargs = [] if ckargs is None else ckargs
            ckkwargs = {} if ckkwargs is None else ckkwargs
            checkfunc = getattr(self, "_checkooi_%s" % check)
            checkfunc(*ckargs, **ckkwargs)

    def _checkooi_ref_exists(self, objtype, attr, target_type, **kwargs):
        if objtype not in self.ooi_objects:
            msg = "ref_exists: %s not a valid object type" % (objtype)
            self.warnings.append(("GENERAL", msg))
            return
        ot_objects = self.ooi_objects[objtype]
        if target_type not in self.ooi_objects:
            msg = "ref_exists: %s not a valid target object type" % (target_type)
            self.warnings.append(("GENERAL", msg))
            return
        ottarg_objects = self.ooi_objects[target_type]

        refattrset = set()
        total_ref = 0

        for obj_key,obj in ot_objects.iteritems():
            ref_attr = obj.get(attr, None)
            if ref_attr is None:
                #msg = "ref_exists: %s.%s attribute is None" % (objtype, attr)
                #self.warnings.append((obj_key, msg))
                continue
            elif type(ref_attr) is list:
                for rattval in ref_attr:
                    refattrset.add(rattval)
                    total_ref += 1
                    if rattval not in ottarg_objects:
                        msg = "ref_exists: %s.%s (list) contains a non-existing object reference (value=%s)" % (objtype, attr, rattval)
                        self.warnings.append((obj_key, msg))
            else:
                refattrset.add(ref_attr)
                total_ref += 1
                if ref_attr not in ottarg_objects:
                    msg = "ref_exists: %s.%s not an existing object reference (value=%s)" % (objtype, attr, ref_attr)
                    self.warnings.append((obj_key, msg))

        log.debug("_checkooi_ref_exists: Checked %s objects type %s against type %s" % (len(ot_objects), objtype, target_type))
        log.debug("_checkooi_ref_exists: Different references=%s (of total=%s) vs target objects=%s" % (len(refattrset), total_ref, len(ottarg_objects)))

    def _parse_date(self, datestr, default=None):
        res_date = None
        try:
            res_date = datetime.datetime.strptime(datestr, "%Y-%m-%d")
        except Exception as ex:
            pass
        if not res_date:
            try:
                res_date = datetime.datetime.strptime(datestr, "%Y-%m")
            except Exception as ex:
                pass
        if not res_date:
            try:
                res_date = datetime.datetime.strptime(datestr, "%Y")
            except Exception as ex:
                pass
        if not res_date and default:
            res_date = default
        elif not res_date:
            raise Exception("Invalid date string: %s" % datestr)
        return res_date

    def _get_child_devices(self):
        """Returns a dict of device to child device ids (nodes and instruments)"""
        node_objs = self.get_type_assets("node")
        inst_objs = self.get_type_assets("instrument")

        res_tree = {}

        for node_id, node_obj in node_objs.iteritems():
            parent_id = node_obj.get("parent_id", None)
            if node_id != parent_id:
                res_tree.setdefault(parent_id, []).append(node_id)
        for inst_id, inst_obj in inst_objs.iteritems():
            ooi_rd = OOIReferenceDesignator(inst_id)
            res_tree.setdefault(ooi_rd.node_rd, []).append(inst_id)

        return res_tree

    @classmethod
    def is_cabled(cls, ooi_rd):
        """Returns True if given RD is associated with the cabled infrastructure"""
        if isinstance(ooi_rd, str):
            ooi_rd = OOIReferenceDesignator(ooi_rd)
        return ooi_rd.marine_io == "RSN" or ooi_rd.subsite_rd in {"CE02SHBP", "CE04OSBP", "CE04OSHY"}

    @classmethod
    def is_dataagent(cls, ooi_rd):
        """Returns True if given RD is serviced by a dataset agent"""
        if isinstance(ooi_rd, str):
            ooi_rd = OOIReferenceDesignator(ooi_rd)
        cabled = cls.is_cabled(ooi_rd)
        if not cabled:
            return True
        if ooi_rd.inst_class in {"HYDBB", "HYDLF", "OBSBB", "OBSBK", "OBSSP", "FLOBN", "OSMOI"}:
            return True
        return False

    def get_agent_code(self, ooi_rd):
        # TODO: This mirrors the get_agent_definition() in the ion_loader - redundancy
        if isinstance(ooi_rd, str):
            ooi_rd = OOIReferenceDesignator(ooi_rd)
        nodetype_objs = self.get_type_assets("nodetype")
        series_objs = self.get_type_assets("series")

        if ooi_rd.rd_type == "asset" and ooi_rd.rd_subtype == "instrument":
            series_obj = series_objs[ooi_rd.series_rd]
            agent_map = series_obj.get("agentmap", [])
        elif ooi_rd.rd_type == "asset" and ooi_rd.rd_subtype == "node":
            nodetype_obj = nodetype_objs[ooi_rd.node_type]
            agent_map = nodetype_obj.get("agentmap", [])
        else:
            raise BadRequest("Must provide instrument or node RD: %s" % ooi_rd.rd)

        is_da = self.is_dataagent(ooi_rd)
        if agent_map:
            for agent_id, rd_prefix in agent_map:
                if ooi_rd.rd.startswith(rd_prefix):
                    return agent_id

        if ooi_rd.rd_subtype == "instrument":
            return series_obj["dart_code"] if is_da else series_obj["ia_code"]
        elif ooi_rd.rd_subtype == "node":
            pa_code = nodetype_obj.get("pa_code", None)
            if pa_code:
                return pa_code
            return "DART_" + ooi_rd.node_type if is_da else ooi_rd.node_type

    def _add_D1000(self):
        class_objs = self.get_type_assets("class")
        series_objs = self.get_type_assets("series")
        makemodel_objs = self.get_type_assets("makemodel")
        instagent_objs = self.get_type_assets("instagent")

        ppsdn_obj = class_objs["PPSDN"]
        d1000_obj = ppsdn_obj.copy()
        d1000_obj.update({"id": "D1000", "name": "Thermistor", "ClassLongName": "Thermistor", "alt_name": "Thermistor",
                          "description": "Thermistor for RASFL and PPSDN instruments",
                          "makemodel": ["non-commercial D1000"]})
        class_objs["D1000"] = d1000_obj

        ppsdna_obj = series_objs["PPSDNA"]
        d1000a_obj = ppsdna_obj.copy()
        d1000a_obj.update({"id": "D1000A", "name": "D1000 Thermistor",
                           "description": "Thermistor for RASFL and PPSDN instruments",
                           "Class": "D1000", "Alternate Instrument Class Name": "Thermistor", "ClassLongName": "Thermistor",
                           "ia_code": "D1000", "makemodel": "non-commercial D1000"})
        series_objs["D1000A"] = d1000a_obj

        mmppsdn_obj = makemodel_objs["non-commercial PPSDN"]
        mmd1000_obj = mmppsdn_obj.copy()
        mmd1000_obj.update({"id": "non-commercial D1000", "name": "non-commercial D1000",
                            "Make_Model_Description": "non-commercial D1000", "Manufacturer": "non-commercial"})
        makemodel_objs["non-commercial D1000"] = mmd1000_obj

        iad1000_obj = instagent_objs.get("D1000", None)
        if iad1000_obj:
            iad1000_obj["inst_class"] = "D1000"
            iad1000_obj["series_list"] = ["D1000A"]
            iad1000_obj["tier1"] = False

    def _post_process(self):
        node_objs = self.get_type_assets("node")
        nodetypes = self.get_type_assets('nodetype')
        subsites = self.get_type_assets('subsite')
        osites = self.get_type_assets('osite')
        sites = self.get_type_assets('site')
        ssites = self.get_type_assets('ssite')
        inst_objs = self.get_type_assets("instrument")
        series_objs = self.get_type_assets("series")
        pagent_objs = self.get_type_assets("platformagent")

        # Add class and series for the D1000 - see Jira CISWMI-230
        self._add_D1000()

        # Make sure all node types have a name
        for code, obj in nodetypes.iteritems():
            if not obj.get('name', None):
                obj['name'] = "(" + code + ")"

        # Add rd and parents to ssites. Bounding box
        for key, ssite in ssites.iteritems():
            subsite_rd_list = ssite['subsite_rd_list']
            if not 'lat_north' in ssite or not ssite['lat_north']:
                subsite_objs = [subsites[subsite_id] for subsite_id in subsite_rd_list]
                bbox = GeoUtils.calc_bounding_box_for_points(subsite_objs, key_mapping=dict(depth="depth_subsite"))
                ssite.update(bbox)
            ssite['rd'] = subsite_rd_list[0]
            ooi_rd = OOIReferenceDesignator(subsite_rd_list[0])
            site = sites[ooi_rd.site_rd]
            osite = osites[site['osite']]
            if 'ssite_list' not in osite:
                osite['ssite_list'] = []
            osite['ssite_list'].append(key)
            ssite['parent_id'] = osite['site_rd_list'][0]

        # Add rd to osites. Bounding box
        for key, osite in osites.iteritems():
            site_rd_list = osite['site_rd_list']
            ssite_list = osite.get('ssite_list', [])

            ssite_objs = [ssites[ss_id] for ss_id in ssite_list]
            bbox = GeoUtils.calc_bounding_box_for_boxes(ssite_objs)

            osite.update(bbox)
            osite['rd'] = site_rd_list[0]

        self.child_devices = self._get_child_devices()

        # Post-process "node" objects:
        # - Make sure all nodes have a name, geospatial coordinates and platform agent connection info
        # - Convert available node First Deploy Date and override date into datetime objects
        for node_id, node_obj in node_objs.iteritems():
            if not node_obj.get("in_mapping", False):
                log.warn("Node %s has no entry in mapping spreadsheet", node_id)
            if not node_obj.get('name', None):
                name = subsites[node_id[:8]]['name'] + " - " + nodetypes[node_id[9:11]]['name']
                node_obj['name'] = name
            if not node_obj.get('latitude', None):
                # Get bbox from child devices
                ch_nodes = self.child_devices.get(node_id, [])  # This gets child nodes and instruments
                node_lats = [float(node_objs[nid]["latitude"]) for nid in ch_nodes if node_objs[nid].get("latitude", None)]
                node_lons = [float(node_objs[nid]["longitude"]) for nid in ch_nodes if node_objs[nid].get("longitude", None)]
                node_deps = [float(node_objs[nid]["depth_subsite"]) for nid in ch_nodes if node_objs[nid].get("depth_subsite", None)]
                if not node_obj.get("latitude", None) and node_lats:
                    node_obj["latitude"] = str(min(node_lats)) + "," + str(max(node_lats))
                if not node_obj.get("longitude", None) and node_lons:
                    node_obj["longitude"] = str(min(node_lons)) + "," + str(max(node_lons))
                if not node_obj.get("depth_subsite", None) and node_deps:
                    node_obj["depth_subsite"] = str(min(node_deps)) + "," + str(max(node_deps))
            if not node_obj.get('latitude', None):
                log.warn("Node %s has no geospatial info", node_id)

            pagent_type = node_obj.get('platform_agent_type', "")
            pagent_obj = pagent_objs.get(pagent_type, None)
            if pagent_obj:
                instrument_agent_rt = pagent_obj['rt_data_path'] == "Direct"
                data_agent_rt = pagent_obj['rt_data_path'] == "File Transfer"
                data_agent_recovery = pagent_obj['rt_data_acquisition'] == "Partial"
                node_obj['instrument_agent_rt'] = instrument_agent_rt
                node_obj['data_agent_rt'] = data_agent_rt
                node_obj['data_agent_recovery'] = data_agent_recovery

            if 'deployment_start' not in node_obj:
                log.warn("Node %s appears not in mapping spreadsheet - inconsistency?!", node_id)
            if not node_obj['in_saf']:
                # Get date from SAF subsite
                subsite_obj = subsites[node_id[:8]]
                node_deploy_date = subsite_obj.get('First Deployment Date', None)
            else:
                # Parse SAF date
                node_deploy_date = node_obj.get('First Deployment Date', None)
            node_obj['SAF_deploy_date'] = self._parse_date(node_deploy_date, DEFAULT_MAX_DATE)
            # Parse override date if available or set to SAF date
            node_obj['deploy_date'] = self._parse_date(node_obj.get('deployment_start', None), node_obj['SAF_deploy_date'])

        # Check all series are in spreadsheet
        for series_id, series_obj in series_objs.iteritems():
            if series_obj.get("tier1", None) is None:
                log.warn("Series %s appears not in mapping spreadsheet - inconsistency?!", series_id)

        # Post-process "instrument" objects:
        # - Set connection info based on platform platform agent
        # - Convert available instrument First Deploy Date into datetime objects
        for inst_id, inst_obj in inst_objs.iteritems():
            inst_rd = OOIReferenceDesignator(inst_id)
            # Parse override date if available or set to SAF date
            inst_obj['SAF_deploy_date'] = self._parse_date(inst_obj.get('First Deployment Date', None), DEFAULT_MAX_DATE)
            inst_obj['deploy_date'] = self._parse_date(inst_obj.get('deployment_start', None), inst_obj['SAF_deploy_date'])

            # Set instrument connection info based on node platform agent connection and instrument agent
            series_obj = series_objs[inst_rd.series_rd]

            node_id = inst_rd.node_rd
            node_obj = node_objs[node_id]
            pagent_type = node_obj['platform_agent_type']
            pagent_obj = pagent_objs[pagent_type]

            # Make sure geospatial values are set or inherited from node
            inst_obj['latitude'] = inst_obj['latitude'] or node_obj['latitude']
            inst_obj['longitude'] = inst_obj['longitude'] or node_obj['longitude']
            inst_obj['depth_port_min'] = inst_obj['depth_port_min'] or node_obj['depth_subsite'].split(",", 1)[0]
            inst_obj['depth_port_max'] = inst_obj['depth_port_max'] or node_obj['depth_subsite'].split(",", 1)[-1]

        # Create SAF node clones with all instruments
        new_nodes, new_insts = [], []
        for node_id, node_obj in node_objs.iteritems():
            clone_rdstr = node_obj.get("clone_rd", None)
            if not clone_rdstr:
                continue
            node_rd = OOIReferenceDesignator(node_id)
            # The parent clone object is already in the node list because of the assetmappings row
            clone_obj = node_objs.get(clone_rdstr, None)
            if not clone_obj:
                log.warn("Node %s: clone node %s not found!", node_id, clone_rdstr)
                continue
            log.info("Cloning node %s from %s, recursively", node_id, clone_rdstr)
            # Set attributes from clone unless already present
            node_obj.update({k: v for k, v in clone_obj.iteritems() if not node_obj.get(k, None)})
            # Recursively clone child devices
            def clone_child(chdev):
                if chdev in inst_objs:
                    chdev_obj = inst_objs[chdev]
                    clonech_obj = chdev_obj.copy()
                    new_insts.append(clonech_obj)
                elif chdev in node_objs:
                    # chdev_obj = node_objs[chdev]
                    # clonech_obj = chdev_obj.copy()
                    # new_nodes.append(clonech_obj)
                    raise BadRequest("Cannot clone platform with child nodes")
                else:
                    raise BadRequest("Child device not found: %s" % chdev)
                # Build new child RD - not trivial
                clonech_rdstr = "%s-%s" % (node_rd.node_rd, clonech_obj["id"][15:])
                clonech_obj["id"] = clonech_rdstr
                log.debug("Cloning %s into %s", chdev, clonech_rdstr)

                # Recurse child devices
                for chdev1 in self.child_devices.get(chdev, []):
                    clone_child(chdev1)
            for chdev in self.child_devices.get(clone_rdstr, []):
                clone_child(chdev)

        # Create SAF instrument clones
        for inst_id, inst_obj in inst_objs.iteritems():
            clone_rdstr = inst_obj.get("clone_rd", None)
            if not clone_rdstr:
                continue
            inst_rd = OOIReferenceDesignator(inst_id)
            # The clone object is already in the node list because of the assetmappings row
            clone_obj = inst_objs.get(clone_rdstr, None)
            if not clone_obj:
                log.warn("Instrument %s: clone instrument %s not found!", inst_id, clone_rdstr)
                continue
            log.info("Cloning instrument %s from %s", inst_id, clone_rdstr)
            inst_obj.update({k: v for k, v in clone_obj.iteritems() if not inst_obj.get(k, None)})

        # Add clones to list of instruments
        if new_nodes or new_insts:
            node_objs.update({no["id"]: no for no in new_nodes})
            inst_objs.update({io["id"]: io for io in new_insts})
            self.child_devices = self._get_child_devices()

    def get_marine_io(self, ooi_rd_str):
        ooi_rd = OOIReferenceDesignator(ooi_rd_str)
        if ooi_rd.error:
            return None
        else:
            return ooi_rd.marine_io

    def get_org_ids(self, ooi_rd_list):
        if not ooi_rd_list:
            return ""
        marine_ios = set()
        for ooi_rd in ooi_rd_list:
            marine_io = self.get_marine_io(ooi_rd)
            if marine_io == "CG":
                marine_ios.add("MF_CGSN")
            elif marine_io == "RSN":
                marine_ios.add("MF_RSN")
            elif marine_io == "EA":
                marine_ios.add("MF_EA")
        return ",".join(marine_ios)

    def delete_ooi_assets(self):
        ooi_asset_types = ['InstrumentModel',
                           'PlatformModel',
                           'Observatory',
                           'Subsite',
                           'PlatformSite',
                           'InstrumentSite',
                           'InstrumentAgent',
                           'InstrumentAgentInstance',
                           'InstrumentDevice',
                           'PlatformAgent',
                           'PlatformAgentInstance',
                           'PlatformDevice',
                           'Deployment',
                           'DataProduct'
        ]

        self.resource_ds = DatastoreManager.get_datastore_instance(DataStore.DS_RESOURCES, DataStore.DS_PROFILE.RESOURCES)

        del_objs = {}
        del_assocs = {}
        all_objs = self.resource_ds.find_by_view("_all_docs", None, id_only=False, convert_doc=False)
        for obj_id, key, obj in all_objs:
            if obj_id.startswith("_design") or not isinstance(obj, dict):
                continue
            obj_type = obj.get("type_", None)
            if obj_type and obj_type in ooi_asset_types:
                del_objs[obj_id] = obj
        for obj_id, key, obj in all_objs:
            if obj_id.startswith("_design") or not isinstance(obj, dict):
                continue
            obj_type = obj.get("type_", None)
            if obj_type == "Association":
                if obj['o'] in del_objs or obj['s'] in del_objs:
                    del_assocs[obj_id] = obj
        for doc in del_objs.values():
            doc_id, doc_rev = doc['_id'], doc['_rev']
            doc.clear()
            doc.update(dict(_id=doc_id, _rev=doc_rev, _deleted=True))
        for doc in del_assocs.values():
            doc_id, doc_rev = doc['_id'], doc['_rev']
            doc.clear()
            doc.update(dict(_id=doc_id, _rev=doc_rev, _deleted=True))

        self.resource_ds.update_doc_mult(del_objs.values())
        self.resource_ds.update_doc_mult(del_assocs.values())

        log.info("Deleted %s OOI resources and %s associations", len(del_objs), len(del_assocs))

    def analyze_ooi_assets(self, end_date):
        """
        Iterates through OOI assets and determines relevant ones by a cutoff data.
        Prepares a report and export for easier development.
        """
        report_lines = []
        node_objs = self.get_type_assets("node")
        nodetype_objs = self.get_type_assets("nodetype")
        inst_objs = self.get_type_assets("instrument")
        series_objs = self.get_type_assets("series")
        instagent_objs = self.get_type_assets("instagent")
        dataagent_objs = self.get_type_assets("dataagent")
        pagent_objs = self.get_type_assets("platformagent")

        deploy_platforms = {}
        platform_children = {}
        self._asset_counts = dict(platform=0, node=0, instd=0, insti=0)

        # Pass:
        # - Check node parent-child deployment date and warn if SAF inconsistencies exist
        # - Determine platform and child first deployment dates
        for ooi_id, ooi_obj in node_objs.iteritems():
            platform_id = ooi_obj['platform_id']
            platform_node = node_objs[platform_id]
            node_deploy_date = ooi_obj['deploy_date']
            platform_deploy_date = platform_node.get('deploy_date')

            if node_deploy_date < platform_deploy_date:
                #log.warn("Child node %s deploy date %s earlier than platform %s deploy date %s",
                #         ooi_id, node_deploy_date, platform_id, platform_deploy_date)
                ooi_obj['deploy_date'] = platform_deploy_date

            # Extract parent-child hierarchy
            if ooi_obj.get('parent_id', None):
                parent_id = ooi_obj.get('parent_id')
                if parent_id not in platform_children:
                    platform_children[parent_id] = []
                platform_children[parent_id].append(ooi_id)

            if not end_date or node_deploy_date <= end_date:
                if ooi_id == platform_id:
                    deploy_platforms[ooi_id] = ooi_obj
                    self._asset_counts["platform"] += 1

            nodetype_obj = nodetype_objs[ooi_id[9:11]]
            nodetype_obj["deploy_date"] = min(ooi_obj['deploy_date'], nodetype_obj.get("deploy_date", None) or DEFAULT_MAX_DATE)

        deploy_platform_list = deploy_platforms.values()
        deploy_platform_list.sort(key=lambda obj: [obj['deploy_date'], obj['name']])

        # Pass: Find instruments by node, set first deployment date
        # - Adjust instrument deployment dates to minimum of parent node (platform) date
        inst_by_node = {}
        isite_by_node = {}
        for inst_id, inst_obj in inst_objs.iteritems():
            ooi_rd = OOIReferenceDesignator(inst_id)
            node_id = ooi_rd.node_rd
            node_obj = node_objs[node_id]
            node_deploy_date = node_obj["deploy_date"]

            # Register instrument to find later
            if node_id not in inst_by_node:
                inst_by_node[node_id] = []
            inst_by_node[node_id].append(ooi_rd.series_rd)
            if node_id not in isite_by_node:
                isite_by_node[node_id] = []
            isite_by_node[node_id].append(inst_id)

            # Find possible override instrument deploy date from InstAvail tab
            series_obj = series_objs[ooi_rd.series_rd]
            series_first_avail = series_obj.get("first_avail", None)

            if series_first_avail:
                inst_obj['deploy_date'] = max(inst_obj['deploy_date'], series_first_avail)
            else:
                inst_obj['deploy_date'] = DEFAULT_MAX_DATE  # If not in override, ignore
            inst_obj['deploy_date'] = max(inst_obj['deploy_date'], node_deploy_date)
            series_obj["deploy_date"] = min(inst_obj['deploy_date'], series_obj.get("deploy_date", None) or DEFAULT_MAX_DATE)


        # Compose the report
        report_lines.append((0, "OOI ASSET REPORT - DEPLOYMENT UNTIL %s" % end_date.strftime('%Y-%m-%d') if end_date else "PROGRAM END"))
        report_lines.append((0, "Platforms by deployment date:"))
        deploy_instruments = {}
        deploy_dataproducts = {}       # Such as DENSITY_L2
        deploy_dataproducttypes = {}   # Such as DENSITY

        for ooi_obj in deploy_platform_list:
            def follow_node_inst(node_id, level):
                inst_lines = []
                inst_series = set()
                for inst_id in isite_by_node.get(node_id, []):
                    inst_obj = inst_objs[inst_id]
                    inst_rd = OOIReferenceDesignator(inst_id)
                    patype = node_objs[node_id]['platform_agent_type']
                    deploy_date = inst_obj.get('deploy_date', DEFAULT_MAX_DATE)
                    iatype, datype = None, None
                    is_data = self.is_dataagent(inst_rd)
                    if is_data:
                        datype = self.get_agent_code(inst_rd)
                        dataagent_obj = dataagent_objs[datype] if datype else None
                        acode = datype if datype else "undefined"
                    else:
                        iatype = self.get_agent_code(inst_rd)
                        instagent_obj = instagent_objs[iatype] if iatype else None
                        acode = iatype if iatype else "undefined"

                    di_dict = dict(
                        is_data=is_data,
                        acode=acode,
                        series=inst_rd.series_rd,
                        patype=patype,
                        series_patype=patype + ":" + inst_rd.series_rd,
                        iatype=iatype,
                        ia_ready=instagent_obj['present'] if iatype else False,
                        ia_active=instagent_obj['active'] if iatype else False,
                        da_ready=dataagent_obj['present'] if datype else False,
                        da_active=dataagent_obj['active'] if datype else False,
                        )
                    qualifiers = []
                    if is_data:
                        if datype and not di_dict['da_ready']: qualifiers.append("NOT_READY")
                        if datype and not di_dict['da_active']: qualifiers.append("NOT_ACTIVE")
                    else:
                        if iatype and not di_dict['ia_ready']: qualifiers.append("NOT_READY")
                        if iatype and not di_dict['ia_active']: qualifiers.append("NOT_ACTIVE")

                    if not end_date or deploy_date <= end_date:
                        deploy_instruments[inst_id] = di_dict
                        self._asset_counts["instd"] += 1
                        inst_series.add((inst_rd.series_rd, None))
                        inst_lines.append((2, "%s               +-%s: %s %s (%s)" % (
                            "  " * level, inst_id, "DA" if is_data else "IA", acode, ", ".join(qualifiers))))
                    else:
                        qualifiers.insert(0, "DEPLOY_POSTPONED")
                        self._asset_counts["insti"] += 1
                        inst_lines.append((2, "%s               +-%s: %s %s (%s)" % (
                            "  " * level, inst_id, "DA" if is_data else "IA", acode, ", ".join(qualifiers))))

                    for dp in inst_obj.get("data_product_list", []):
                        if dp not in deploy_dataproducts:
                            deploy_dataproducts[dp] = []
                        deploy_dataproducts[dp].append(inst_id)
                        dpt,_ = dp.rsplit("_", 1)
                        if dpt not in deploy_dataproducttypes:
                            deploy_dataproducttypes[dpt] = set()
                        deploy_dataproducttypes[dpt].add(inst_rd.series_rd)
                return sorted(inst_lines), inst_series

            def follow_child_nodes(level, child_nodes=None):
                if not child_nodes:
                    return
                for ch_id in child_nodes:
                    ch_obj = node_objs[ch_id]
                    deploy_date = ch_obj.get('deploy_date', DEFAULT_MAX_DATE)
                    if not end_date or deploy_date <= end_date:
                        self._asset_counts["node"] += 1
                        chnode_id = ch_obj['id']
                        chnode_rd = OOIReferenceDesignator(chnode_id)
                        is_data = self.is_dataagent(chnode_rd)
                        if is_data:
                            datype = self.get_agent_code(chnode_rd)
                            acode = datype if datype else "undefined"
                        else:
                            patype = self.get_agent_code(chnode_rd)
                            acode = patype if patype else "undefined"

                        inst_lines, inst_series = follow_node_inst(ch_id, level)
                        inst_series_list = ", ".join([i for i,p in sorted(list(inst_series))])
                        report_lines.append((1, "%s             +-%s %s %s: %s %s (%s)" % ("  "*level, chnode_id,
                                                                                   ch_obj['name'], ch_obj.get('platform_agent_type', ""),
                                                                                   "DA" if is_data else "PA", acode, inst_series_list)))
                        report_lines.extend(inst_lines)
                        follow_child_nodes(level+1, platform_children.get(ch_id,None))

            pnode_id = ooi_obj['id']
            pnode_rd = OOIReferenceDesignator(pnode_id)
            is_data = self.is_dataagent(pnode_rd)
            if is_data:
                datype = self.get_agent_code(pnode_rd)
                acode = datype if datype else "undefined"
            else:
                patype = self.get_agent_code(pnode_rd)
                acode = patype if patype else "undefined"
            inst_lines, inst_series = follow_node_inst(ooi_obj['id'], 0)
            inst_series_list = ", ".join([i for i,p in sorted(list(inst_series))])
            report_lines.append((0, "  %s %s %s %s: %s %s (%s)" % (ooi_obj['deploy_date'].strftime('%Y-%m-%d'), pnode_id, ooi_obj['name'],
                                                           ooi_obj.get('platform_agent_type', ""),
                                                           "DA" if is_data else "PA", acode, inst_series_list)))
            report_lines.extend(inst_lines)

            follow_child_nodes(0, platform_children.get(ooi_obj['id'], None))

        #import pprint
        #pprint.pprint(deploy_instruments)

        report_lines.append((0, "Asset Counts:"))
        report_lines.append((0, "  Platforms: %s" % len(deploy_platforms)))
        report_lines.append((1, "    Assembly/component nodes: %s" % self._asset_counts["node"]))
        report_lines.append((1, "    Instruments (deployed): %s" % len(deploy_instruments)))
        report_lines.append((1, "    Instruments (postponed): %s" % self._asset_counts["insti"]))
        ser_list = self._get_unique(deploy_instruments, "series")
        report_lines.append((0, "  Instrument models (unique): (%s) %s" % (len(ser_list), ",".join(ser_list))))
        ser_list = self._get_unique(deploy_instruments, "series", "iart", True)
        report_lines.append((0, "  Instrument models (RT inst agent): (%s) %s" % (len(ser_list), ",".join(ser_list))))
        ser_list = self._get_unique(deploy_instruments, "series", "dart", True)
        report_lines.append((0, "  Instrument models (RT data agent): (%s) %s" % (len(ser_list), ",".join(ser_list))))

        agent_list = self._get_unique(deploy_instruments, "iatype", "iart", True)
        report_lines.append((0, "  Instrument agent types: (%s) %s" % (len(agent_list), ",".join(agent_list))))
        ready_agent_list = self._get_unique(deploy_instruments, "iatype", "ia_ready", True)
        report_lines.append((0, "    Ready types: (%s) %s" % (len(ready_agent_list), ",".join(ready_agent_list))))

        agent_list = self._get_unique(deploy_instruments, "iatype", "dart", True)
        report_lines.append((0, "  RT data agent types: (%s) %s" % (len(agent_list), ",".join(agent_list))))

        serpa_list = self._get_unique(deploy_instruments, "series_patype")
        report_lines.append((0, "  Instrument model x Platform type combinations: %s" % (len(serpa_list))))

        patypes = self._get_unique(deploy_instruments, "patype")
        for patype in patypes:
            series = self._get_unique(deploy_instruments, "series", "patype", patype)
            report_lines.append((1, "    %s: (%s) %s" % (patype, len(series), ",".join(series))))
        report_lines.append((0, "  Data product types: (%s) %s" % (len(deploy_dataproducttypes), ",".join(sorted(deploy_dataproducttypes.keys())))))
        report_lines.append((0, "  Data product variants: (%s) %s" % (len(deploy_dataproducts), ",".join(sorted(deploy_dataproducts.keys())))))
        # for dpt in sorted(deploy_dataproducttypes.keys()):
        #     dpt_series = deploy_dataproducttypes[dpt]
        #     report_lines.append((1, "    %s: (%s) %s" % (dpt, len(dpt_series), ",".join(sorted(dpt_series)))))



        self.asset_report = report_lines
        self.deploy_platforms = deploy_platforms
        self.deploy_instruments = deploy_instruments
        self.deploy_dataproducts = deploy_dataproducts

    def _get_unique(self, dict_obj, key, fkey=None, fval=None, sort=True, count=False):
        vals = set()
        for obj in dict_obj.values():
            kv = obj.get(key, None)
            fkv = obj.get(fkey, None)
            if kv and (not fkey or fkv == fval):
                vals.add(kv)
        if count:
            return len(vals)
        return sorted(list(vals)) if sort else vals

    def _count(self, dict_obj, key, value=True):
        count = 0
        for obj in dict_obj.values():
            kv = obj.get(key, None)
            if kv == value:
                count += 1
        return count

    def report_ooi_assets(self, report_level=5, dump_assets=True, print_report=True):
        if print_report:
            print "\n".join(line for level, line in self.asset_report if level < report_level)

        if dump_assets:
            self._dump_assets()

    def _dump_assets(self):
        from ion.util.datastore.resources import ResourceRegistryHelper
        rrh = ResourceRegistryHelper()
        rrh.dump_dicts_as_xlsx(self.ooi_objects)
