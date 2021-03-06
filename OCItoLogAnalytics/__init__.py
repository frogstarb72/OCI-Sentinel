import datetime
import logging

import azure.functions as func
from azure.common.credentials import ServicePrincipalCredentials
from azure.loganalytics import LogAnalyticsDataClient
from azure.loganalytics.models import QueryBody


import json
import requests
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import base64
import oci
import os

def main(mytimer: func.TimerRequest) -> None:
    utc_timestamp = datetime.utcnow().replace(
        tzinfo=timezone.utc).isoformat()

    if mytimer.past_due:
        logging.info('The timer is past due!')

    logging.info('Python timer trigger function ran at %s', utc_timestamp)
    initOCI()


def initOCI():

    # Set up OCI config
    #config = oci.config.from_file(
    #    os.environ["OCI_PATH_TO_CONFIG"],
    #    "DEFAULT")

    config = get_config()

    # Create a service client
    identity = oci.identity.IdentityClient(config)

    tenancy_id = config["tenancy"]
    # Update the customer ID to your Log Analytics workspace ID
    customer_id = os.environ["LOG_ANALYTICS_CUSTID"]

    # For the shared key, use either the primary or the secondary Connected Sources client authentication key   
    shared_key = os.environ["LOG_ANALYTICS_KEY"]
    log_type = os.environ["LOG_ANALYTICS_LOGTYPE"]

    #  Initiate the client with the locally available config.
    identity = oci.identity.IdentityClient(config)

    #  Timespan defined by variables start_time and end_time(today).
    #  ListEvents expects timestamps into RFC3339 format.
    #  For the purposes of sample script, logs of last 5 days.
    end_time = datetime.utcnow()

    # Query Log Analytics for lastest date/time in OCIAudit table and pass.
    start_time = get_start_time(log_type)
    print('Start time is: {0}'.format(start_time))
 
    # This array will be used to store the list of available regions.
    regions = get_subscription_regions(identity, tenancy_id)

    # This array will be used to store the list of compartments in the tenancy.
    compartments = get_compartments(identity, tenancy_id)

    # Initialize the audit client 
    audit = oci.audit.audit_client.AuditClient(config)

    #  For each region get the logs for each compartment.
    for r in regions:
        #  Initialize with the current region value.
        audit.base_client.set_region(r)

        #  Get audit events for the current region which is specified in the audit object.
        get_audit_events(customer_id,
            shared_key,
            audit,
            compartments,
            start_time,
            end_time)



# Copyright (c) 2016, 2020, Oracle and/or its affiliates.  All rights reserved.
# This software is dual-licensed to you under the Universal Permissive License (UPL) 1.0 as shown at https://oss.oracle.com/licenses/upl or Apache License 2.0 as shown at http://www.apache.org/licenses/LICENSE-2.0. You may choose either license.

#  This script retrieves all audit logs across an Oracle Cloud Infrastructure Tenancy.
#  for a timespan defined by start_time and end_time.
#  This sample script retrieves Audit events for last 5 days.
#  This script will work at a tenancy level only.

def get_subscription_regions(identity, tenancy_id):

    # To retrieve the list of all available regions.
    list_of_regions = []
    list_regions_response = identity.list_region_subscriptions(tenancy_id)
    for r in list_regions_response.data:
        list_of_regions.append(r.region_name)
    return list_of_regions


def get_compartments(identity, tenancy_id):

    # Retrieve the list of compartments under the tenancy.
    list_compartments_response = oci.pagination.list_call_get_all_results(
        identity.list_compartments,
        compartment_id=tenancy_id).data

    compartment_ocids = [c.id for c in filter(lambda c: c.lifecycle_state == 'ACTIVE', list_compartments_response)]
    # Add the root compartment 
    compartment_ocids.append(tenancy_id)

    return compartment_ocids


def get_audit_events(customer_id, shared_key, audit, compartment_ocids, start_time, end_time):
    '''
    # Get events iteratively for each compartment defined in 'compartments_ocids'
    # for the region defined in 'audit'.
    # This method eagerly loads all audit records in the time range and it does
    # have performance implications of lot of audit records.
    # Ideally, the generator method in oci.pagination should be used to lazily
    # load results.
    '''

    log_type = os.environ["LOG_ANALYTICS_LOGTYPE"]

    for c in compartment_ocids:
        # change here to get one page at a time and write events rather than getting
        # all pages in a compartment, then getting all compartments then returning
        # the entire set
        for paged_audit_events in oci.pagination.list_call_get_all_results_generator(
                audit.list_events,
                yield_mode='response',
                compartment_id=c,
                start_time=start_time,
                end_time=end_time
            ):
        
            for event in paged_audit_events.data: 
                jsondoc = json.loads(str(event))
                parsed_json = json.dumps(jsondoc, indent=4, sort_keys=True)
                #print("The event time is: {0}".format(jsondoc["event_time"]))
                # We use the event_time in OCI as the Date Generated in Log Analytics
                post_data(customer_id, shared_key, parsed_json, log_type)
 


def build_signature(customer_id, shared_key, date, content_length, method, content_type, resource):
    x_headers = 'x-ms-date:' + date
    string_to_hash = method + "\n" + str(content_length) + "\n" + content_type + "\n" + x_headers + "\n" + resource
    bytes_to_hash = bytes(string_to_hash, encoding="utf-8") 
    decoded_key = base64.b64decode(shared_key)
    encoded_hash = base64.b64encode(hmac.new(decoded_key, bytes_to_hash, digestmod=hashlib.sha256).digest()).decode()
    authorization = f"SharedKey {customer_id}:{encoded_hash}"
    return authorization

# Build and send a request to the POST API
def post_data(customer_id, shared_key, body, log_type):
    method = 'POST'
    content_type = 'application/json'
    resource = '/api/logs'
    # Conversion of the OCI event time to RFC 1123 date format string
    rfc1123date = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
    content_length = len(body)
    signature = build_signature(customer_id, shared_key, rfc1123date, content_length, method, content_type, resource)
    uri = 'https://' + customer_id + '.ods.opinsights.azure.com' + resource + '?api-version=2016-04-01'
    print('URI : ' + uri)
    headers = {
        'content-type': content_type,
        'Authorization': signature,
        'Log-Type': log_type,
        'x-ms-date': rfc1123date
    }
    response = requests.post(uri, data=body, headers=headers)
    if (response.status_code >= 200 and response.status_code <= 299):
        print('Log Analytics Event Accepted - OCI Event Time: {0}'.format(rfc1123date))
    else:
        logging.error("Log Analytics returned a {0}, headers: {1}, body:\n {2}".format(response.status_code, headers, body))
        print(response.status_code)



def get_start_time(log_type):
    workspace_id = os.environ["LOG_ANALYTICS_CUSTID"] # from the log analytics workspace
    
    # Use Managed Service Identity if available once the management SDKs are GA
    #from azure.identity import DefaultAzureCredential
    #credentials = DefaultAzureCredential()

    # Use a service principal that is granted permission in Log Analytics
    credentials = ServicePrincipalCredentials(
        client_id = os.environ["AZURE_CLIENT_ID"],
        secret = os.environ["AZURE_CLIENT_SECRET"],
        tenant = os.environ["AZURE_TENANT_ID"],
        resource = "https://api.loganalytics.io "
    )
    try:
        client = LogAnalyticsDataClient(credentials, base_url=None)

        body = QueryBody(query = "union isfuzzy=true ({0}_CL |  summarize arg_max(event_time_t , event_time_t ) | project event_time_t ) | summarize arg_max(event_time_t , event_time_t ) | project event_time_t".format(log_type)) # the query

        query_results = client.query(workspace_id, body) # type: https://github.com/Azure/azure-sdk-for-python/blob/master/sdk/loganalytics/azure-loganalytics/azure/loganalytics/models/query_results.py
        table = query_results.tables[0] # https://github.com/Azure/azure-sdk-for-python/blob/master/sdk/loganalytics/azure-loganalytics/azure/loganalytics/models/table.py

        rows = table.rows # [][] of arbitrary data

        start_row = rows[0]
        start_time = start_row[0]

        start_datetime = datetime.strptime(start_time,'%Y-%m-%dT%H:%M:%S.%fZ')
    except:
        #Go back 30 days from now if the start time cannot be parsed to a valid date time
        start_datetime = datetime.utcnow() + timedelta(days=-30)

    return start_datetime
    

def get_config():
    # Create configuration dictionary for OCI
    key_content = os.environ["OCI_KEY_CONTENT"]

    config = {
        "user": os.environ["USER_OCID"],
        "fingerprint": os.environ["OCI_FINGERPRINT"],
        "tenancy": os.environ["OCI_TENANCY"],
        "key_content": key_content,
        "region": os.environ["OCI_REGION"],
        "pass_phrase": os.environ["OCI_PASS_PHRASE"]
    }

    return config