import cPickle
import httplib
from datetime import datetime
from datetime import timedelta
import json
import time

from cloudshell.core.context.error_handling_context import ErrorHandlingContext
from cloudshell.cp.vcenter.common.vcenter.vmomi_service import pyVmomiService
from cloudshell.devices.driver_helper import get_api
from cloudshell.devices.driver_helper import get_cli
from cloudshell.devices.driver_helper import get_logger_with_thread_id
from cloudshell.shell.core.driver_context import AutoLoadDetails
from cloudshell.shell.core.resource_driver_interface import ResourceDriverInterface
from cloudshell.traffic.teravm.api.client import TeraVMClient
from cloudshell.traffic.teravm.vchassis.configuration_attributes_structure import TeraVMTrafficGeneratorVChassisResource
from cloudshell.traffic.teravm.vchassis.runners.configuration_runner import TeraVMConfigurationRunner
from pyVim.connect import SmartConnect, Disconnect
from requests.exceptions import HTTPError


VCENTER_RESOURCE_USER_ATTR = "User"
VCENTER_RESOURCE_PASSWORD_ATTR = "Password"
PORT_MAC_ADDRESS_ATTR = "CS_VirtualTrafficGeneratorPort.MAC Address"
SSH_SESSION_POOL = 1
ASSOCIATED_MODELS = ["TeraVM Virtual Blade"]
SERVICE_STARTING_TIMEOUT = 60 * 60
API_STARTING_TIMEOUT = 30 * 60
SSH_STARTING_TIMEOUT = 60 * 60
MGMT_IP_TIMEOUT = 30 * 60


class TeraVMVirtualChassisDriver(ResourceDriverInterface):
    SHELL_TYPE = "CS_VirtualTrafficGeneratorChassis"
    SHELL_NAME = "TeraVM Virtual Chassis"

    def __init__(self):
        """
        ctor must be without arguments, it is created with reflection at run time
        """
        self._cli = None

    def initialize(self, context):
        """
        Initialize the driver session, this function is called everytime a new instance of the driver is created
        This is a good place to load and cache the driver configuration, initiate sessions etc.
        :param InitCommandContext context: the context the command runs on
        """
        self._cli = get_cli(SSH_SESSION_POOL)
        return "Finished initializing"

    @staticmethod
    def _get_resource_attribute_value(resource, attribute_name):
        """

        :param resource cloudshell.api.cloudshell_api.ResourceInfo:
        :param str attribute_name:
        """
        for attribute in resource.ResourceAttributes:
            if attribute.Name.lower() == attribute_name.lower():
                return attribute.Value

    @staticmethod
    def _get_mgmt_ip_addr(vsphere, si, vmuid, mgmt_netwok_name, logger):
        """

        :param vsphere:
        :param si:
        :param vmuid:
        :param mgmt_netwok_name:
        :return:
        """
        timeout_time = datetime.now() + timedelta(seconds=MGMT_IP_TIMEOUT)

        while True:
            logger.info("Trying to retrieve controller MGMT IP...")
            vm = vsphere.get_vm_by_uuid(si, vmuid)

            for net in vm.guest.net:
                if net.network.lower() == mgmt_netwok_name.lower():
                    mgmt_ip = net.ipConfig.ipAddress[0].ipAddress
                    logger.info("MGMT IP is {}".format(mgmt_ip))
                    return mgmt_ip

            if datetime.now() > timeout_time:
                raise Exception("TeraVM Controller service didn't get MGMT IP Address within {} minute(s)"
                                .format(MGMT_IP_TIMEOUT / 60))

            time.sleep(10)

    @staticmethod
    def _wait_for_service_deployment(tvm_api_client, logger):
        """

        :param tvm_api_client:
        :param logger:
        :return:
        """
        timeout_time = datetime.now() + timedelta(seconds=SERVICE_STARTING_TIMEOUT)

        while not tvm_api_client.check_if_service_is_deployed(logger):
            logger.info("Waiting for controller service start...")

            if datetime.now() > timeout_time:
                raise Exception("TeraVM Controller service didn't start within {} minute(s)"
                                .format(SERVICE_STARTING_TIMEOUT / 60))
            time.sleep(10)

    def _configure_executive_server(self, resource_config, tvm_api_client, logger):
        """

        :param resource_config:
        :param tvm_api_client:
        :param logger:
        :return:
        """
        timeout_time = datetime.now() + timedelta(seconds=API_STARTING_TIMEOUT)

        while True:
            logger.exception("Trying to configure Executive Server via API....")

            try:
                tvm_api_client.configure_executive_server(ip_addr=resource_config.executive_server)
            except HTTPError as e:
                if e.response.status_code == httplib.BAD_GATEWAY:
                    logger.exception("Unable to configure Executive Server via API")

                    if datetime.now() > timeout_time:
                        raise Exception("Unable to perform configure Executive Server API operation within {} minute(s)"
                                        .format(API_STARTING_TIMEOUT / 60))
                else:
                    raise
            else:
                logger.exception("Executive Server was successfully configured via API")
                return

            time.sleep(5 * 60)

    def _execute_cli_configuration(self, resource_config, cs_api, logger):
        """

        :param resource_config:
        :param cs_api:
        :param logger:
        :return:
        """
        timeout_time = datetime.now() + timedelta(seconds=SSH_STARTING_TIMEOUT)

        while True:
            logger.exception("Trying to configure License Server via CLI....")

            try:
                configuration_operations = TeraVMConfigurationRunner(resource_config=resource_config,
                                                                     cli=self._cli,
                                                                     cs_api=cs_api,
                                                                     logger=logger)

                configuration_operations.configure_license_server(license_server_ip=resource_config.license_server)

            except Exception as e:  # todo: at least match specific exception
                logger.exception("Unable to configure License Server via CLI")

                if datetime.now() > timeout_time:
                    raise Exception("Unable to perform configure License Server CLI operation within {} minute(s)"
                                    .format(SSH_STARTING_TIMEOUT / 60))
            else:
                logger.exception("License Server was successfully configured via CLI")
                return

            time.sleep(5 * 60)

    @staticmethod
    def _find_module_by_mac(modules, mac_address, logger):
        """

        :param modules:
        :param mac_address:
        :return:
        """
        mac_address = mac_address.lower()

        for module in modules:
            if module["macAddress"].lower() == mac_address:
                return module

        logger.error("Unable to find Module with MAC Address: {}. Models info from controller: {}"
                     .format(mac_address, modules))

        raise Exception("Unable to find Module with MAC Address {}".format(mac_address))

    def configure_device_command(self, context, resource_cache):
        """Configure Virtual Chassis

        :param ResourceCommandContext context: the context the command runs on
        :type resource_cache: str
        """
        logger = get_logger_with_thread_id(context)
        logger.info('Configure device command started')

        with ErrorHandlingContext(logger):
            cs_api = get_api(context)

            resource_config = TeraVMTrafficGeneratorVChassisResource.from_context(context=context,
                                                                                  shell_type=self.SHELL_TYPE,
                                                                                  shell_name=self.SHELL_NAME)

            # get VM uuid of the Deployed App
            deployed_vm_resource = cs_api.GetResourceDetails(resource_config.fullname)
            vmuid = deployed_vm_resource.VmDetails.UID
            logger.info("Deployed TVM Controller App uuid: {}".format(vmuid))

            # get vCenter name
            app_request_data = json.loads(context.resource.app_context.app_request_json)
            vcenter_name = app_request_data["deploymentService"]["cloudProviderName"]
            logger.info("vCenter shell resource name: {}".format(vcenter_name))

            vsphere = pyVmomiService(SmartConnect, Disconnect, task_waiter=None)

            # get vCenter credentials
            vcenter_resource = cs_api.GetResourceDetails(resourceFullPath=vcenter_name)
            user = self._get_resource_attribute_value(resource=vcenter_resource,
                                                      attribute_name=VCENTER_RESOURCE_USER_ATTR)

            encrypted_password = self._get_resource_attribute_value(resource=vcenter_resource,
                                                                    attribute_name=VCENTER_RESOURCE_PASSWORD_ATTR)

            password = cs_api.DecryptPassword(encrypted_password).Value

            logger.info("Connecting to the vCenter: {}".format(vcenter_name))
            si = vsphere.connect(address=vcenter_resource.Address, user=user, password=password)

            mgmt_address = self._get_mgmt_ip_addr(vsphere=vsphere,
                                                  si=si,
                                                  vmuid=vmuid,
                                                  mgmt_netwok_name=resource_config.tvm_mgmt_network,
                                                  logger=logger)

            logger.info("Updating resource address for the chassis to {}".format(mgmt_address))
            cs_api.UpdateResourceAddress(context.resource.fullname, mgmt_address)
            resource_config.address = mgmt_address

            api_password = cs_api.DecryptPassword(resource_config.api_password).Value

            tvm_api_client = TeraVMClient(address=mgmt_address,
                                          user=resource_config.api_user,
                                          password=api_password)

            logger.info("Waiting for Service to be deployed... ")
            self._wait_for_service_deployment(tvm_api_client, logger)


            logger.info("Configuring Executive Server...")
            self._configure_executive_server(resource_config=resource_config,
                                             tvm_api_client=tvm_api_client,
                                             logger=logger)

            resources = cPickle.loads(resource_cache)

            # update vBlades deployed Apps
            modules = tvm_api_client.get_modules_info()

            for deployed_app in resources.values():
                if deployed_app.ResourceModelName in ASSOCIATED_MODELS:
                    module = self._find_module_by_mac(modules=modules, mac_address=deployed_app.Address, logger=logger)
                    cs_api.UpdateResourceAddress(deployed_app.Name, "{}/M{}".format(mgmt_address, module["number"]))

                    port_map = {}

                    for port in deployed_app.ChildResources:
                        mac_address = self._get_resource_attribute_value(resource=port,
                                                                         attribute_name=PORT_MAC_ADDRESS_ATTR).lower()
                        port_map[mac_address] = port

                    for test_agent in module["testAgents"]:
                        for test_if in test_agent["testInterfaces"]:
                            mac_addr = test_if["macAddress"].lower()
                            port = port_map[mac_addr]
                            cs_api.UpdateResourceAddress(port.Name, "P{}".format(test_if["number"]))

            logger.info("Executing CLI configuration commands")

            self._execute_cli_configuration(resource_config=resource_config,
                                            cs_api=cs_api,
                                            logger=logger)

    def get_inventory(self, context):
        """Discovers the resource structure and attributes.

        :param cloudshell.shell.core.driver_context.AutoLoadCommandContext context: the context the command runs on
        :return Attribute and sub-resource information for the Shell resource you can return an AutoLoadDetails object
        :rtype: cloudshell.shell.core.driver_context.AutoLoadDetails
        """
        logger = get_logger_with_thread_id(context)
        logger.info('Autoload started')

        with ErrorHandlingContext(logger):
            return AutoLoadDetails([], [])

    def cleanup(self):
        """

        :return:
        """
        pass


if __name__ == "__main__":
    import mock
    from cloudshell.shell.core.context import ResourceCommandContext, ResourceContextDetails, ReservationContextDetails

    address = '192.168.42.242'

    user = 'cli'
    password = 'diversifEye'
    api_user = "admin"
    api_password = "admin"
    port = 443
    scheme = "https"
    auth_key = 'h8WRxvHoWkmH8rLQz+Z/pg=='
    api_port = 8029

    context = ResourceCommandContext()
    context.resource = ResourceContextDetails()
    context.resource.name = "TVM-C-14.1_db2e-ccbb"
    context.resource.fullname = "TVM-C-14.1_db2e-ccbb"
    context.reservation = ReservationContextDetails()
    context.reservation.reservation_id = "f49c75a2-860d-446d-b912-6cc99531ccbb"
    context.resource.attributes = {}

    for attr, value in [("User", user),
                        ("Password", password),
                        ("API User", user),
                        ("API Password", password),
                        ("TVM Comms Network", "TVM_Comms"),
                        ("TVM MGMT Network", "VM Network"),
                        ("Executive Server", "192.168.42.177"),
                        ("License Server", "192.168.42.164")]:

        context.resource.attributes["{}.{}".format(TeraVMVirtualChassisDriver.SHELL_NAME, attr)] = value

    context.resource.address = address
    context.resource.app_context = mock.MagicMock(app_request_json=json.dumps(
        {
            "deploymentService": {
                "cloudProviderName": "vcenter"
            }
        }))

    context.connectivity = mock.MagicMock()
    context.connectivity.server_address = "192.168.85.22"

    dr = TeraVMVirtualChassisDriver()
    dr.initialize(context)

    print dr.configure_device_command(context, "")

