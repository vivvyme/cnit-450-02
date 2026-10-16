#!/usr/bin/env python3

import serial
import time
import re
import os

#TODO: device detection
#TODO: import password from system variables? i just removed it for now

password = ""

def main():
    port = serial.Serial(port='COM3')
    logon(port, password)
    fhand = open(os.path.dirname(os.path.abspath(__file__)) + '/addresses.txt')
    for address in fhand:
        pinginate(port, address)


def send_to_console(ser: serial.Serial, command: str, wait_time: float = 0.5):
    command_to_send = command + "\r"
    ser.write(command_to_send.encode('utf-8'))
    time.sleep(wait_time)
    return readAll(ser)

def readAll(ser = serial.Serial):
    return (ser.read(ser.inWaiting()).decode('utf-8')) 

def pinginate(ser: serial.Serial, address: str):
    command = "ping " + address + "\r"
    ser.write(command.encode("utf-8"))
    out = readAll(ser)
    printL(out)
    check = re.compile('#$')
    while (check.match(out) == None):
        #time.sleep(.1)
        out = readAll(ser)
        printL(out)

def printL(text: str) :
    print(text, end="")

def logon(ser: serial.Serial, passwd: str):
    check = send_to_console(ser, "", 1)
    printL(check)
    if '#' not in check:
        if ">" not in check:
            printL(send_to_console(ser, passwd, 1))
        printL(send_to_console(ser, 'en', 1))
        printL(send_to_console(ser, passwd, 1))
    if ')' in check:
        printL(send_to_console(ser, 'end'))
    printL(send_to_console(ser, ""))

main()