#!/usr/bin/env python3

import serial
from yaml import CLoader as Loader, CDumper as Dumper
import yaml
import time
import re
import os

fileName = 'addresses.yaml'

def main():
    port = serial.Serial(port='COM3')
    logon(port)
    name, area = deviceName(port)
    print('Device area: ' + area)
    print('Device name: ' + name)
    list = assembleList(name, area)
    printL('IP List: ')
    for address in list:
        printL(address + ' ')
    print()
    #printL(send_to_console(port, ""))
    
    interestingList = []

    for address in list:
        check = pinginate(port, address)
        if check == 'noRouting':
            break
        elif check == 'interesting':
            interestingList.append(address)
        elif check == 'success':
            continue
    if interestingList == []:
        print(area + '-' + name + ': All addresses pinged successfully')
    else:
        printL(area + '-' + name + ': ' + str(len(interestingList)) + ' interesting devices: ')
        for address in interestingList:
            printL(address + ' ')
        print()


def assembleList(name : str, area : str):
    fhand = open(os.path.dirname(os.path.abspath(__file__)) + '/' + fileName)
    addressList = yaml.load(fhand, Loader)
    allList = addressList['all']
    if "None" in allList:
        allList = []
        print('Universal ping list empty, skipping')
    try:
        areaList = addressList[area]['all']
        if "None" in addressList[area]['all']:
            areaList = []
            print('No addresses in area, skipping')
    except:
        print('Unrecognized area, skipping')
        areaList = []
    try:
        deviceList = addressList[area][name]
        if "None" in addressList[area][name]:
            deviceList = []
            print('No addresses for device, skipping')
    except:
        print('Unrecognized device, skipping')
        deviceList = []

    completeList = allList + areaList + deviceList
    for item in completeList:
        if '#' in item:
            completeList.remove(item)
    
    return completeList

def deviceName(ser: serial.Serial):
    read = send_to_console(ser, "")[2:-1].split('-')
    return (read[1] + '-' + read[2]), read[0]

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
    output = ''
    out = readAll(ser)
    output = output + out
    printL(address + ':\t')
    check = re.compile('#$')
    
    while (check.match(out) == None):
        #time.sleep(.1)
        out = readAll(ser)
        output = output + out
    
    if ("protocol not running" not in output):
        results = output.split('\n')[3]
        printL(results)
        if results.count('!') < 3 or not results[4] == '!':
            printL('\t\t\t(!)')
            print()
            return 'interesting'
        else:
            print()
            return 'success'
    else:
        print('[ERROR] IP Routing not enabled on connected device! Are you plugged into the right one?')
        return 'noRouting'

def printL(text: str) :
    print(text, flush= True, end="")

def logon(ser: serial.Serial):
    check = send_to_console(ser, "", 1)
    if '#' not in check:
        print("Device needs sign-in")
        passwd = input("Please enter device password: ")
        if ">" not in check:
            send_to_console(ser, passwd, 1)
        send_to_console(ser, 'en', 1)
        send_to_console(ser, passwd, 1)
    if ')' in check:
        send_to_console(ser, 'end')

main()