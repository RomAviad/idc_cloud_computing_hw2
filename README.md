# IDC Cloud Computing HW2
### Second homework assignment for Cloud Computing course in IDC Herzliya.

## Task Description
In this exercise, you’ll need to build a caching system to store users’ session data. The session data is addressable by key (the user id) and can range in size between
0.5 KB – 8 KB of data.
The API you’ll need to support is composed of the following functions:
* `put (str_key, data, expiration_date)`
* `get(str_key)` → `null` or `data`

You can expose these functions as rest endpoints, or use any RPC mechanism you desire.

The code behind these endpoints will run on multiple EC2 instances and you need to orchestrate the data between them.

To pass, your solution needs to be able to handle, without data loss, the following scenarios:
* Putting and getting data across multiple keys, that will reside in different nodes.
* Losing a node and not losing any data
* Adding a new node to the system and extending its capacity

The client of the caching service has a single endpoint that it is aware of, you cannot do client-side load balancing. You are free to do load balancing in your code or using the cloud infrastructure to do that.

You do not have to worry about persistence, the data can be kept purely in memory.

You do not have to worry about consistency or concurrency, a read may get an older version of the data, as long as eventually we’ll read the latest version.

# My Solution
* Python as the language of choice
* `Flask` as a basis for HTTP serving
* Deployment script is in python, using `boto3`
* Cache entries are held in-memory, sharded across the live nodes with redundancy.
* Cache entries are persisted as objects on AWS S3
  * Data-loss resistant, even in case all of the nodes that hold a specific key in-memory crash.
* Redis (on ElastiCache) is used by this project's cache nodes to report heartbeat
  * Easy to keep track on how many nodes we have live and (re)distribute cached data
    
## Run instructions
### First-run dependencies:
* If you don't have AWS CLI installed, The `init.sh` script should get you going on a linux machine.
* The deployment scripts rely on the `boto3` python package. It already comes with AWS CLI, but you can go double-safe 
by running `pip install boto3` on your terminal. 


## Deployment

### Initial Deployment
In order to have the entire deployment set-up, run the following command on your terminal: 
```shell script
python deployment.py
```
After a few minutes the deployment will be up and running.
When deployment finished successfully you would have the following format printed out:
```
APP DEPLOYMENT DONE!
ENDPOINT: <your load-balancer endpoint>
RUN_ID: <generated run_id (to be used for adding more instances later on>
S3 BUCKET: <deployment target S3 bucket>
PEM FILE: <name of the pem file created with the key-pair of the deployment>
REDIS HOST: <redis endpoint (used for cache node heartbeat checking across nodes)>
```

### Adding an instance to an existing deployment
In order to add another instance behind the load balancer endpoint of your deployment, run the following command 
on your terminal:
```shell script
python deployment.py --run_id <run_id> --add_instance
```


## Using the cache service
The cache service holds three endpoints:

* GET `/health` - Making sure that the service is up and running
* GET `/keys/<your key>` - Corresponds to the `get` requirement in the task description. expired keys will return `null`
* PUT `/keys/<your key>` - Corresponsd to the `put` requirement in the task description. Data should be passed as a JSON
    body. Python example:
 ```python
import requests

base_url = "http://my-dummy-elb.us-east-1.elb.amazonaws.com"
body = {
    "data": {"a field": "with some value", "yet another field": 123},
    "expiration_date": "2021-06-22T20:22" # ISO FORMAT datetime string
}
put_response = requests.put(f"{base_url}/keys/test_key", json=body)
assert put_response.ok
print(put_response.json())
```