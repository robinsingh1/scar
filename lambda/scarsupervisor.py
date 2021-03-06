# Copyright (C) GRyCAP - I3M - UPV
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import boto3
import json
import os
import re
from subprocess import call, check_output, STDOUT
import traceback

print('Loading function')

udocker_bin = "/tmp/udocker/udocker"
lambda_output = "/tmp/lambda-stdout.txt"
script = "/tmp/udocker/script.sh"
container_name = 'lambda_cont'
init_script_path = "/tmp/udocker/init_script.sh"

def prepare_environment(aws_request_id):
    # Install udocker in /tmp
    os.makedirs("/tmp/udocker", exist_ok=True)    
    call(["cp", "/var/task/udocker", udocker_bin])
    call(["chmod", "u+rx", udocker_bin])
    os.makedirs("/tmp/home/.udocker", exist_ok=True)    
    os.makedirs("/tmp/%s/output" % aws_request_id, exist_ok=True)  
    if ('INIT_SCRIPT_PATH' in os.environ) and os.environ['INIT_SCRIPT_PATH']:
        call(["cp", "/var/task/init_script.sh", init_script_path])

def prepare_container(container_image):
    # Check if the container is already downloaded
    cmd_out = check_output([udocker_bin, "images"]).decode("utf-8")
    if container_image not in cmd_out:
        print("SCAR: Pulling container '%s' from dockerhub" % container_image)
        # If the container doesn't exist
        call([udocker_bin, "pull", container_image])
    else:
        print("SCAR: Container image '%s' already available" % container_image)
    # Download and create container
    cmd_out = check_output([udocker_bin, "ps"]).decode("utf-8")
    if container_name not in cmd_out:
        print("SCAR: Creating container with name '%s' based on image '%s'." % (container_name, container_image))
        call([udocker_bin, "create", "--name=%s" % container_name, container_image])
        # Set container execution engine to Fakechroot
        call([udocker_bin, "setup", "--execmode=F1", container_name])
    else:
        print("SCAR: Container '" + container_name + "' already available")

def check_alpine_image():
    home = os.environ['UDOCKER_DIR']
    musl_path = "%s/containers/%s/ROOT/lib/libc.musl-x86_64.so.1" % (home, container_name)
    print("PATH: %s" % musl_path)
    if os.path.isfile(musl_path):
        print("Alpine image found. Using busybox to execute scripts.")
        return "/bin/busybox sh"
    else:
        return "/bin/sh"        

def add_global_variable(variables, key, value):
    variables.append('--env')
    variables.append(key + '=' + value)
    return variables
        
def get_global_variables():
    variables = []
    for key in os.environ.keys():
        # Find global variables with the specified prefix
        if re.match("CONT_VAR_.*", key):
            variables = add_global_variable(variables, key.replace("CONT_VAR_", ""), os.environ[key])
    # Add IAM credentials
    if not ('CONT_VAR_AWS_ACCESS_KEY_ID' in os.environ):
        variables = add_global_variable(variables, "AWS_ACCESS_KEY_ID", os.environ["AWS_ACCESS_KEY_ID"])
    if not ('CONT_VAR_AWS_SECRET_ACCESS_KEY' in os.environ):
        variables = add_global_variable(variables, "AWS_SECRET_ACCESS_KEY", os.environ["AWS_SECRET_ACCESS_KEY"])
    # Always add Session and security tokens
    variables = add_global_variable(variables, "AWS_SESSION_TOKEN", os.environ["AWS_SESSION_TOKEN"])
    variables = add_global_variable(variables, "AWS_SECURITY_TOKEN", os.environ["AWS_SECURITY_TOKEN"])
    return variables

def prepare_output(context):
    stdout = "SCAR: Log group name: %s\n" % context.log_group_name
    stdout += "SCAR: Log stream name: %s\n" % context.log_stream_name
    stdout += "---------------------------------------------------------------------------\n"
    return stdout

def create_file(content, path):
    with open(path, "w") as f:
        f.write(content)

def create_event_file(event, request_id):
    event_file_path = "/tmp/%s/" % request_id
    os.makedirs(event_file_path, exist_ok=True)     
    create_file(event, event_file_path + "/event.json")

def pre_process(event, context):
    create_event_file(json.dumps(event), context.aws_request_id)
    prepare_environment(context.aws_request_id)
    prepare_container(os.environ['IMAGE_ID'])
    check_event_records(event, context)

def check_event_records(event, context):
    request_id = context.aws_request_id
    if(Utils().is_s3_event(event)):
        s3_records = Utils().get_s3_records(event)
        for s3_record in s3_records:
            S3_Bucket().download_input(s3_record, request_id)

def post_process(event, context):
    request_id = context.aws_request_id
    if(Utils().is_s3_event(event)):
        S3_Bucket().upload_output(event['Records'][0]['s3'], request_id)
        call(["rm", "-rf", "/tmp/%s/output/" % request_id])
                
def create_command(event, context):
    # Create container execution command
    command = [udocker_bin, "--quiet", "run"]
    container_dirs = ["-v", "/tmp", "-v", "/dev", "-v", "/proc", "-v", "/etc/hosts", "--nosysdirs"]
    container_vars = ["--env", "REQUEST_ID=%s" % context.aws_request_id]
    command.extend(container_dirs)
    command.extend(container_vars)
    
    # Add global variables (if any)
    global_variables = get_global_variables()
    if global_variables:
        command.extend(global_variables)

    # Use the correct script executable 
    script_exec = check_alpine_image()

    # Container running script
    if ('script' in event) and event['script']:
        create_file(event['script'], script)
        command.extend(["--entrypoint=%s %s" % (script_exec, script), container_name])
    # Container with args
    elif ('cmd_args' in event) and event['cmd_args']:
        args = map(lambda x: x.encode('ascii'), event['cmd_args'])
        command.append(container_name)
        command.extend(args)
    # Script to be executed every time (if defined)
    elif ('INIT_SCRIPT_PATH' in os.environ) and os.environ['INIT_SCRIPT_PATH']:
        command.extend(["--entrypoint=%s %s" % (script_exec, init_script_path), container_name])
    # Only container
    else:
        command.append(container_name)
    return command
    
def lambda_handler(event, context):
    print("SCAR: Received event: " + json.dumps(event))
    stdout = prepare_output(context)
    try:
        pre_process(event, context)
        # Create container execution command
        command = create_command(event, context)
        print ("Udocker command: %s" % command)
        # Execute script
        call(command, stderr=STDOUT, stdout=open(lambda_output, "w"))

        stdout += check_output(["cat", lambda_output]).decode("utf-8")
        
        post_process(event, context)
        
    except Exception:
        stdout += "ERROR: Exception launched:\n %s" % traceback.format_exc()
    print(stdout)
    return stdout

class Utils():
    def is_s3_event(self, event):
        if ('Records' in event) and event['Records']:
            # Check if the event is an S3 event
            if event['Records'][0]['eventSource'] == "aws:s3":
                return True
        else:
            return False
    
    def get_s3_records(self, event):
        records = []
        if ('Records' in event) and event['Records']:
            for record in event['Records']:
                if('s3' in record) and record['s3']:
                    records.append(record['s3'])
        return records

class S3_Bucket():
    
    def get_s3_client(self):
        return boto3.client('s3')

    def get_bucket_name(self, s3_record):
        return s3_record['bucket']['name']
    
    def download_input(self, s3_record, request_id):
        bucket_name = self.get_bucket_name(s3_record)
        file_key = s3_record['object']['key']
        download_path = '/tmp/%s/%s' % (request_id, file_key)
        print ("Downloading item from bucket %s with key %s" % (bucket_name, file_key))
        os.makedirs(os.path.dirname(download_path), exist_ok=True)        
        self.get_s3_client().download_file(bucket_name, file_key, download_path)

    def upload_output(self, s3_record, request_id):
        bucket_name = self.get_bucket_name(s3_record)
        output_folder = "/tmp/%s/output/" % request_id
        output_files_path = self.get_all_files_in_directory(output_folder)
        for file_path in output_files_path:
            file_key = "output/%s" % file_path.replace(output_folder, "")
            print ("Uploading file to bucket %s with key %s" % (bucket_name, file_key))
            self.get_s3_client().upload_file(file_path, bucket_name, file_key)
            print ("Changing ACLs for public-read for object in bucket %s with key %s" % (bucket_name, file_key))
            s3_resource = boto3.resource('s3')
            obj = s3_resource.Object(bucket_name, file_key)
            obj.Acl().put(ACL='public-read')

    def get_all_files_in_directory(self, dir_path):
        files = []
        for dirname, dirnames, filenames in os.walk(dir_path):
            for filename in filenames:
                files.append(os.path.join(dirname, filename))
        return files
