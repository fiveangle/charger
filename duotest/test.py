# testing module, to check understanding of modbus-tk with iCharger 4010 DUO USB HUD interface

import modbus_tk
import modbus_tk.defines as cst
from modbus_tk.modbus import Query
from modbus_tk.modbus_rtu import RtuMaster
from modbus_tk.exceptions import ModbusInvalidRequestError, ModbusInvalidResponseError
from modbus_tk.utils import get_log_buffer
from modbus_tk import LOGGER

import usb.core
import usb.util
import struct
import sys

ICHARGER_VENDOR_ID = 0x483
ICHARGER_PRODUCT_ID = 0x5751
END_POINT_ADDRESS_WRITE = 0x01
END_POINT_ADDRESS_READ = 0x81
MAX_READWRITE_LEN = 64

READ_REG_COUNT_MAX = 30
WRITE_REG_COUNT_MAX = 28

#
#
# relies on the following going into /etc/udev/rules.d/10-icharger4010.rules
#
# apply user land permissions so we don't require root to read/write it
# SUBSYSTEMS=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="5751", MODE:="0666"
#
#

class iChargerQuery(Query):
    """
    Subclass of a Query. Adds the Modbus specific part of the protocol for iCharger over USB, which uses
    a rather specific protocol format to send the PDU.

    Please note - read/writes are limited to 64 bytes, whereby the PDU is prefixed with two bytes, <ADU len>
    and 0x30 respectively.

    The 'slave' portion of the protocol goes unused because, I presume, the iCharger provides only a
    single modbus slave - or because of coconuts.
    """

    def __init__(self):
        """Constructor"""
        super(iChargerQuery, self).__init__()
        self.adu_len = None
        self.start_addr = None
        self.quantity = None

    def build_request(self, pdu, slave):
        """ Constructs the output buffer for the request based on the func_code value """
        (self.func_code, ) = struct.unpack(">B", pdu[0])

        if self.func_code == cst.READ_INPUT_REGISTERS:
            self.adu_len = 7
            (self.start_addr, self.quantity) = struct.unpack(">HH", pdu[1:5])
        else:
            raise ModbusInvalidRequestError("Request func code not recognized (code is: {0})".format(self.func_code))

        return struct.pack(">BB", self.adu_len, 0x30) + pdu

    def parse_response(self, response):
        if len(response) < 3:
            raise ModbusInvalidResponseError("Response length is invalid {0}".format(len(response)))

        # check for max length problem, the iCharger HID based Modbus protocol handles only
        # 64 byte packets.  If you want to read more, then send multiple read requests.
        (self.response_length, self.adu_constant, self.response_func_code) = struct.unpack(">BBB", response[0:3])

        if self.adu_constant != 0x30:
            raise ModbusInvalidResponseError("Response doesn't containt constant 0x30 in ADU portion, constant value found is {0}".format(self.adu_constant))

        if self.response_func_code != self.func_code:
            raise ModbusInvalidResponseError("Response func_code {0} isn't the same as the request func_code {1}".format(
                self.response_func_code, self.func_code
            ))

        # primitive byte swap the entire thing...
        header = response[2:4]
        # LOGGER.debug(get_log_buffer("header <- ", header))

        data = response[4:]
        # LOGGER.debug(get_log_buffer("data <- ", data))

        final = header + ''.join([c for t in zip(data[1::2], data[::2]) for c in t])

        # LOGGER.debug(get_log_buffer("final <- ", final))

        return final

class USBSerialFacade:
    """
    Implements facade such that the ModBus Master thinks it is using a serial
    device when talking to the iCharger via USB-HID.

    USBSerialFacade sets the active USB configuration and claims the interface,
    take note - this must be released when the instance is cleaned up / destroyed.  If
    the USB device cannot be found the facade does nothing.  If the kernel driver cannot
    be detached that's more of a problem and right now the USBSerialFacade throws a big
    exception from the constructor.


    """
    def __init__(self, vendorId, productId):
        self._claimed = False
        self.dev = usb.core.find(idVendor=vendorId, idProduct=productId)
        if self.dev is None:
            return

        if not self._detach_kernel_driver():
            sys.exit("failed to detach kernel driver")

        # don't do this - fails every time on the Pi3, regardless of permissions.
        # self.dev.set_configuration()

        self.cfg = self.dev.get_active_configuration()

    def _detach_kernel_driver(self):
        if self.dev.is_kernel_driver_active(0):
            try:
                self.dev.detach_kernel_driver(0)
            except usb.core.USBError as e:
                return False
        return True

    def _claim_interface(self):
        try:
            usb.util.claim_interface(self.dev, 0)
            self._claimed = True
            return True
        except:
            pass
        return False

    def _release_interface(self):
        try:
            usb.util.release_interface(self.dev, 0)
            self._claimed = False
            return True
        except:
            pass
        return False

    @property
    def serial_number(self):
        return usb.util.get_string(self.dev, self.dev.iSerialNumber) if self.valid else None

    @property
    def is_open(self):
        return self.dev is not None and self._claimed

    @property
    def name(self):
        if self.serial_number is not None:
            return "iCharger 4010 Duo SN:" + self.serial_number
        return "! iCharger Not Connected !"

    def open(self):
        # acquire the interface
        return self._claim_interface()

    def close(self):
        return self._release_interface()

    @property
    def timeout(self):
        return 5000

    @timeout.setter
    def timeout(self, new_timeout):
        pass

    @property
    def baudrate(self):
        """As this is a serial facade, we return a totally fake baudrate here"""
        return 19200

    @property
    def valid(self):
        return self.dev is not None

    def reset_input_buffer(self):
        """There are no internal buffers so this method is a no-op"""
        pass

    def reset_output_buffer(self):
        """There are no internal buffers so this method is a no-op"""
        pass

    def write(self, content):
        if self.dev is not None and self._claimed:
            pad_len = MAX_READWRITE_LEN - len(content)
            self.dev.write(END_POINT_ADDRESS_WRITE, content + ("\0" * pad_len))
        return 0

    def read(self, expected_length):
        if self.dev is not None and self._claimed:
            return self.dev.read(END_POINT_ADDRESS_READ, expected_length).tostring()
        return 0


class iChargerMaster(RtuMaster):
    """
    Modbus master interface to the iCharger, implements higher level routines to get the
    status / channel information from the device.
    """
    def __init__(self, serial):
        super(iChargerMaster, self).__init__(serial)

    def _make_query(self):
        return iChargerQuery()

    def _modbus_read_input_registers(self, addr, format):
        """
        Uses the modbus_tk framework to acquire data from the device.

        The data_format is always specified in native format - DO NOT include '>' or '<' as then
        the byte swapping will not work.  Simply provide format characters.

        The number of words of data being returned is calculated as the byte size of
        the return packet / 2, and the total length of data being read is the total byte size
        + 4 bytes for the header information.  This is an iCharger Modbus protocol specific
        decision by Junsi.

        :param addr: the base address (size is words not bytes)
        :param format: the structure of the data being received, assumes struct.unpack()
        :return: the tuples of unpacked and byte swapped data
        """
        byte_len = struct.calcsize(format)
        quant = byte_len // 2

        """The slave param (1 in this case) is never used, its appropriate to RTU based Modbus
        devices but as this is iCharger via USB-HID this is irrelevant."""
        return self.execute(1,
                            cst.READ_INPUT_REGISTERS,
                            addr,
                            data_format=format,
                            quantity_of_x=quant,
                            expected_length=(quant * 2) + 4)

    def get_device_info(self):
        """
        Returns the following information from the iCharger, known as the 'device only reads message'
        :return: a tuple containing the response of 'device reads only message'
        Device ID (u16)
        Device SN (S8[12])
        Software Version (u16)
        Hardware Version (u16)
        SYSTEM length (u16 - see also SYSTEM storage area)
        MEMORY length (u16)
        ch1 status word (u16)
        ch2 status word (u16)

        The channel 1/2 status words following this bit-mask:
        Bit0-run flag
        Bit1-error flag
        Bit2-control status flag
        Bit3-run status flag
        Bit4-dialog box status flag
        Bit5-cell voltage flag
        Bit6-balance flag
        """
        return self._modbus_read_input_registers(0x000, format="H12sHHHHHH")

    def get_channel_status(self, channel):
        """"
        Returns the following information from the iCharger, known as the 'channel input read only' message:
        :return:
        Timestamp (u32)
        The current output power (u32)
        The current output current (s16)
        The current input voltage (u16)
        The current output voltage (u16)
        The current output capacity (s32)
        The current internal temp (s16)
        The current external temp (s16)
        Cell 0-15 voltage (each is u16, 4010DUO uses only first 10)
        Cell 0-15 balance status (each is u8, 4010DUO uses only first 10)
        Cell 0-15 internal resistance (each is u16, 4010DUO uses only first 10)
        The cells total IR (u16)
        Cycle count (u16)
        Control status (u16)
        Run status (u16)
        Run error (u16)
        Dialog Box ID (u16)
        """
        addr = 0x100 if channel == 1 else 0x200

        # Instead of writing some code to dynamically split the "longer than 64 bytes returned" request
        # into smaller chunks - I just decided to manually split it up into 3 calls :-)
        data1_fmt = "LLhHHlhh"
        data1 = self._modbus_read_input_registers(addr, format=data1_fmt)
        data2_fmt = "HHHHHHHHHHHHHHH"
        data2_addr = addr + (struct.calcsize(data1_fmt) / 2)
        data2 = self._modbus_read_input_registers(data2_addr, data2_fmt)
        data3_fmt = "7cHHHHHHHH"
        data3_addr = data2_addr + (struct.calcsize(data2_fmt) / 2)
        data3 = self._modbus_read_input_registers(data3_addr, data3_fmt)

        return data1 + data2 + data3

def main():
    logger = modbus_tk.utils.create_logger("console")

    master = iChargerMaster(USBSerialFacade(ICHARGER_VENDOR_ID, ICHARGER_PRODUCT_ID))

    master.set_verbose(True)

    res = master.get_device_info()
    LOGGER.info(res)

    LOGGER.info(master.get_channel_status(1))

    LOGGER.info(master.get_channel_status(2))

if __name__ == "__main__":
    main()
