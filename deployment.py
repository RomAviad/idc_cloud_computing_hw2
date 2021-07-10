import datetime
import http
import os

from boto3 import Session
from botocore import exceptions

UBUNTU_20_04_AMI = "ami-042e8287309f5df03"


class CacheAppDeployer(object):
    def __init__(
        self,
        base_ec2_image=UBUNTU_20_04_AMI,
        base_instance_type="t3.micro",
        run_id=None,
    ):
        self._run_id = run_id
        self.session = Session()
        self.ec2_client = self.session.client("ec2")
        self.elb_client = self.session.client("elbv2")
        self.s3_client = self.session.client("s3")
        self._base_image = base_ec2_image
        self._base_instance_type = base_instance_type
        self._elb_name = None

        http_con = http.client.HTTPConnection("ipinfo.io")
        http_con.request("GET", "/ip")
        response = http_con.getresponse()
        self.my_ip = response.read().decode()
        self.redis_host = None
        self._redis_cluster_id = None

    @property
    def run_id(self):
        if self._run_id is None:
            self._run_id = self._create_run_id()
        return self._run_id

    @property
    def elb_name(self):
        if self._elb_name is None:
            self._elb_name = f"{self.run_id}-elb"
        return self._elb_name

    @property
    def redis_cluster_id(self):
        if self._redis_cluster_id is None:
            self._redis_cluster_id = f"hw2-sync-redis-{self.run_id}"
        return self._redis_cluster_id

    @property
    def key_name(self):
        return f"cloud-course-{self.run_id}"

    @property
    def bucket_name(self):
        return f"{self.run_id}-store"

    def _create_run_id(self):
        run_id = int(datetime.datetime.utcnow().timestamp())
        print(f"Created Run ID: {run_id}")
        return run_id

    def create_key_pair(self):
        client = self.ec2_client
        key_name = f"cloud-course-{self.run_id}"
        key_file = f"{key_name}.pem"
        if not os.path.exists(key_file):
            response = client.create_key_pair(KeyName=key_name)
            key_pair_id = response["KeyPairId"]
            with open(key_file, "w") as f:
                f.write(response["KeyMaterial"])
            os.chmod(path=key_file, mode=0o400)
            print(f"Created key-pair file {key_file} ;  Key-pair ID: {key_pair_id}")
        return key_file, key_name

    def create_security_groups(self, vpc_id, elb_arn):
        client = self.ec2_client
        instance_group_name = f"cc-sg-{self.run_id}"
        elb_group_name = f"cc-elb-sg-{self.run_id}"

        vpc = client.describe_vpcs(VpcIds=[vpc_id])
        cidr_block = vpc["Vpcs"][0]["CidrBlock"]

        response = client.create_security_group(
            Description="INSTANCE_ACCESS",
            GroupName=instance_group_name,
        )
        instance_group_id = response["GroupId"]

        print(f"Created Security Group {instance_group_name} ; id: {instance_group_id}")

        ip_permissions = [
            {
                "FromPort": port,
                "IpProtocol": "tcp",
                "IpRanges": [
                    {
                        "CidrIp": f"{self.my_ip}/32",
                        "Description": f"port {port} access from my ip",
                    },
                ],
                "ToPort": port,
            }
            for port in [22, 5000]
        ]
        for port in [5000, 6379]:
            ip_permissions.append(
                {
                    "FromPort": port,
                    "IpProtocol": "tcp",
                    "IpRanges": [
                        {
                            "CidrIp": cidr_block,
                            "Description": f"port {port} access from within security group",
                        },
                    ],
                    "ToPort": port,
                }
            )
        auth_response = client.authorize_security_group_ingress(
            GroupName=instance_group_name,
            IpPermissions=ip_permissions,
        )

        response = client.create_security_group(
            Description="ELB ACCESS",
            GroupName=elb_group_name,
        )
        elb_group_id = response["GroupId"]
        auth_response = client.authorize_security_group_ingress(
            GroupName=elb_group_name,
            IpPermissions=[
                {
                    "FromPort": 80,
                    "IpProtocol": "tcp",
                    "IpRanges": [
                        {
                            "CidrIp": "0.0.0.0/0",
                            "Description": f"HTTP ACCESS TO ELB",
                        },
                    ],
                    "ToPort": 80,
                }
            ],
        )
        self.elb_client.set_security_groups(
            LoadBalancerArn=elb_arn, SecurityGroups=[elb_group_id]
        )

        groups = {
            "instances": (instance_group_name, instance_group_id),
            "elb": (elb_group_name, elb_group_id),
        }
        return groups

    def create_s3_bucket(self):
        client = self.s3_client

        bucket_name = self.bucket_name
        response = client.create_bucket(Bucket=bucket_name)
        return bucket_name

    def create_instance(self, sg_id, key_pair_file, key_name, s3_bucket, redis_address):
        client = self.ec2_client

        response = client.run_instances(
            ImageId=self._base_image,
            InstanceType=self._base_instance_type,
            SecurityGroupIds=[sg_id],
            MinCount=1,
            MaxCount=1,
            KeyName=key_name,
        )

        instance_id = response["Instances"][0]["InstanceId"]
        print(f"Instance {instance_id} created... waiting for it to become available")
        waiter = client.get_waiter("instance_running")
        waiter.wait(InstanceIds=[instance_id])

        describe_response = client.describe_instances(InstanceIds=[instance_id])
        public_ip_address = describe_response["Reservations"][0]["Instances"][0][
            "PublicIpAddress"
        ]
        print(f"Instance {instance_id} is running @ {public_ip_address}")
        print("Deploying code to instance...")

        store_config_file = "CONFIG.txt"
        main_script = "main.sh"
        session_creds = self.session.get_credentials()
        with open(store_config_file, "w") as f:
            f.writelines(
                [
                    f"export STORE_BUCKET={s3_bucket}\n",
                    f"export AWS_ACCESS_KEY_ID={session_creds.access_key}\n",
                    f"export AWS_SECRET_ACCESS_KEY={session_creds.secret_key}\n",
                    f"export REDIS_ADDRESS={redis_address}\n",
                    f"export NODE_IP={public_ip_address}\n",
                ]
            )

        with open(main_script, "w") as f:
            f.writelines(
                ["#! /bin/bash\n" f"source {store_config_file} && python3 main.py\n"]
            )

        scp_command = (
            f'scp -i {key_pair_file} -o "StrictHostKeyChecking=no" -o "ConnectionAttempts=60" '
            f"requirements.txt {store_config_file} {main_script} server/main.py server/cache_ring_management.py ubuntu@{public_ip_address}:/home/ubuntu/"
        )

        os.system(scp_command)

        print("Code deployment done... setting up dependencies...")
        installation_commands = [
            "sudo apt update",
            "sudo apt install python3-pip -y",
            "sudo pip3 install -r requirements.txt",
            f"chmod +x {main_script} && screen -d -m ./{main_script}",
        ]
        ssh_base = f'ssh -i {key_pair_file} -o "StrictHostKeyChecking=no" -o "ConnectionAttempts=10" ubuntu@{public_ip_address}'
        for cmd in installation_commands:
            print(cmd)
            ssh_command = f"{ssh_base} '{cmd}'"
            os.system(ssh_command)
        print(
            f"Code deployed... server is running on instance {instance_id} ({public_ip_address}:5000)"
        )
        return instance_id, public_ip_address

    def create_elb(self):
        client = self.elb_client
        response = None
        try:
            response = client.describe_load_balancers(Names=[self.elb_name])
        except exceptions.ClientError as e:
            if e.response["Error"]["Code"] != "LoadBalancerNotFound":
                raise e
            subnets = self.get_default_subnets()
            response = client.create_load_balancer(
                Name=self.elb_name,
                Scheme="internet-facing",
                IpAddressType="ipv4",
                Subnets=subnets,
            )

        elb_arn = response["LoadBalancers"][0]["LoadBalancerArn"]
        vpc_id = response["LoadBalancers"][0]["VpcId"]
        endpoint = response["LoadBalancers"][0]["DNSName"]

        return elb_arn, vpc_id, endpoint

    def get_default_subnets(self):
        client = self.ec2_client
        response = client.describe_subnets(
            Filters=[{"Name": "default-for-az", "Values": ["true"]}]
        )
        subnet_ids = [s["SubnetId"] for s in response["Subnets"]]
        return subnet_ids

    def create_target_group_and_listeners(self, vpc_id, elb_arn):
        target_group = self.elb_client.create_target_group(
            Name=f"elb-{self.run_id}-tg",
            Protocol="HTTP",
            Port=80,
            VpcId=vpc_id,
            HealthCheckProtocol="HTTP",
            HealthCheckPort="5000",
            HealthCheckPath="/health",
            TargetType="instance",
        )
        target_group_arn = target_group["TargetGroups"][0]["TargetGroupArn"]
        listeners = self.elb_client.describe_listeners(LoadBalancerArn=elb_arn)
        if len(listeners["Listeners"]) == 0:
            self.elb_client.create_listener(
                LoadBalancerArn=elb_arn,
                Protocol="HTTP",
                Port=80,
                DefaultActions=[
                    {
                        "Type": "forward",
                        "TargetGroupArn": target_group_arn,
                        "Order": 100,
                    }
                ],
            )

        return target_group_arn

    def register_instance_in_elb(self, instance_id, target_group_arn):
        self.elb_client.register_targets(
            TargetGroupArn=target_group_arn, Targets=[{"Id": instance_id, "Port": 5000}]
        )

    def get_security_groups(self):
        instance_group_name = f"cc-sg-{self.run_id}"
        elb_group_name = f"cc-elb-sg-{self.run_id}"
        sg_response = self.ec2_client.describe_security_groups(
            GroupNames=[instance_group_name, elb_group_name]
        )

        result = {}
        groups = sg_response["SecurityGroups"]
        for group in groups:
            group_id = group["GroupId"]
            group_name = group["GroupName"]
            if group_name not in {instance_group_name, elb_group_name}:
                continue
            group_key = "instances" if group_name == instance_group_name else "elb"
            result[group_key] = (group_name, group_id)
        return result

    def deploy_app(self):
        # region = get_region()
        # aws_account = get_account()

        key_pair_file, key_name = self.create_key_pair()
        elb_arn, vpc_id, elb_endpoint = self.create_elb()
        security_groups = self.create_security_groups(vpc_id=vpc_id, elb_arn=elb_arn)

        instances_sg_id = security_groups["instances"][1]

        redis_address = self.create_redis(sg_id=instances_sg_id)
        target_group_arn = self.create_target_group_and_listeners(
            vpc_id=vpc_id, elb_arn=elb_arn
        )
        s3_bucket = self.create_s3_bucket()
        instance_id, instance_ip = self.create_instance(
            sg_id=instances_sg_id,
            key_pair_file=key_pair_file,
            key_name=key_name,
            s3_bucket=s3_bucket,
            redis_address=redis_address,
        )

        self.register_instance_in_elb(
            instance_id=instance_id, target_group_arn=target_group_arn
        )

        return elb_endpoint

    def get_target_group(self):
        target_group_response = self.elb_client.describe_target_groups(
            Names=[f"elb-{self.run_id}-tg"],
        )
        target_group_arn = target_group_response["TargetGroups"][0]["TargetGroupArn"]
        return target_group_arn

    def add_instance_to_existing_deployment(self):
        key_name = self.key_name
        key_pair_file = f"{key_name}.pem"
        security_groups = self.get_security_groups()
        bucket_name = self.bucket_name
        redis_address = self.get_redis_address()

        instance_id, instance_ip = self.create_instance(
            sg_id=security_groups["instances"][1],
            key_pair_file=key_pair_file,
            key_name=key_name,
            s3_bucket=bucket_name,
            redis_address=redis_address,
        )

        target_group_arn = self.get_target_group()
        self.register_instance_in_elb(
            instance_id=instance_id, target_group_arn=target_group_arn
        )

    def create_redis(self, sg_id):
        client = self.session.client("elasticache")
        cluster_id = self.redis_cluster_id
        response = client.create_cache_cluster(
            CacheClusterId=cluster_id,
            Engine="redis",
            NumCacheNodes=1,
            CacheNodeType="cache.t3.micro",
            SecurityGroupIds=[sg_id],
        )

        waiter = client.get_waiter("cache_cluster_available")
        waiter.wait(CacheClusterId=cluster_id)

        describe_response = client.describe_cache_clusters(
            CacheClusterId=cluster_id, ShowCacheNodeInfo=True
        )
        redis_address = describe_response["CacheClusters"][0]["CacheNodes"][0][
            "Endpoint"
        ]["Address"]
        self.redis_host = redis_address
        print(f"Created redis cluster with ID {cluster_id}; address: {redis_address}")

        return redis_address

    def get_redis_address(self):
        client = self.session.client("elasticache")
        describe_response = client.describe_cache_clusters(
            CacheClusterId=self.redis_cluster_id, ShowCacheNodeInfo=True
        )
        redis_address = describe_response["CacheClusters"][0]["CacheNodes"][0][
            "Endpoint"
        ]["Address"]
        self.redis_host = redis_address

        return redis_address


if __name__ == "__main__":
    from argparse import ArgumentParser

    parser = ArgumentParser("HW2_DEPLOYEMNT")

    parser.add_argument(
        "--run_id",
        help="existing run id to re-use existing resources",
        required=False,
        default=None,
    )
    parser.add_argument(
        "--add_instance",
        help="add an instance to the deployment",
        action="store_true",
        default=False,
    )

    args = parser.parse_args()

    deployer = CacheAppDeployer(run_id=args.run_id)
    if args.add_instance:
        assert args.run_id, "Cannot add an instance without providing the run ID"
        deployer.add_instance_to_existing_deployment()
        print("ADDING INSTANCE IS DONE")
    else:
        endpoint = deployer.deploy_app()
        print(
            "\n".join(
                [
                    f"APP DEPLOYMENT DONE!",
                    f"ENDPOINT: http://{endpoint}",
                    f"RUN_ID: {deployer.run_id}",
                    f"S3 BUCKET: {deployer.bucket_name}",
                    f"PEM FILE: {deployer.key_name}.pem",
                    f"REDIS HOST: {deployer.redis_host}",
                ]
            )
        )
