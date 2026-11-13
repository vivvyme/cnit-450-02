#!/usr/bin/env python3

#import serial
from yaml import CLoader as Loader, CDumper as Dumper
import yaml
import time
#import re
import os
import netmiko
from netmiko import ConnectHandler
from netmiko import NetMikoAuthenticationException, NetmikoTimeoutException

hostsFile = 'hosts.yaml'
addressFile = 'addresses.yaml'

def main():
    print('Initalizing THE PINGINATOR')
    print()
    print('Gathering hosts...')

    hFile = open(os.path.dirname(os.path.abspath(__file__)) + '/' + hostsFile)
    hostList = yaml.load(hFile, Loader)
    for item in hostList['hosts']:
            if '#' in item:
                hostList['hosts'].remove(item)

    print('Hosts collected: ' + str(hostList['hosts'])[1:-1].replace("'", ""))
    print()

    notable = {}
    for host in hostList['hosts']:
        note, area, device = doThings(host)
        print('--------------------------------------------------------------------')
        if note != 'success':
            if area not in notable:
                notable[area] = {}
            if device not in notable[area]:
                notable[area][device] = {}
            notable[area][device][host] = note
    if notable == {}:
        print('Every device contacted successfully! Woohoo!')
    else:
        print('Something went wrong somewhere!')
        print('Check in on:')
        print(yaml.dump(notable, Dumper=Dumper))
        print()
    

def doThings(host):
    try:
        print('Contacting ' + str(host))
        port = ConnectHandler(
            device_type="cisco_ios",
            host=host,
            username="admin",
            password="enterprise",
            secret="enterprise"
            )
        port.enable()
    except (NetmikoTimeoutException, NetMikoAuthenticationException):
        print('Unable to SSH into ' + host)
        return('sshFailure', 'Unknown area', 'Connection issues')

    #port.send_command('send * \n Hello from the pinginator!')
    
    name, area = deviceName(port)
    print('Device area: ' + area)
    print('Device name: ' + name)
    list = assembleList(name, area)
    print()
    printL('IP List: ')
    for address in list:
        printL(address + ' ')
    print()
    #printL(send_to_console(port, ""))
    
    interestingList = []

    rValue = 'success'

    for address in list:
        check = pinginate(port, address)
        if check == 'noRouting':
            rValue = 'fucked'
            break
        elif check == 'interesting':
            interestingList.append(address)
        elif check == 'success':
            continue
    print()
    if interestingList == []:
        print(area + '-' + name + ': All addresses pinged successfully')
    else:
        printL(area + '-' + name + ': ' + str(len(interestingList)) + ' interesting devices: ')
        for address in interestingList:
            printL(address + ' ')
        print()
        rValue = ('Missed ping on ' + str(interestingList)[1:-1].replace("'", ""))
    return(rValue, area, name)


def assembleList(name : str, area : str):
    fhand = open(os.path.dirname(os.path.abspath(__file__)) + '/' + addressFile)
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

def deviceName(ser: ConnectHandler):
    read = ser.find_prompt()[:-1].split('-')
    return (read[1] + '-' + read[2]), read[0]

def send_to_console(ser: ConnectHandler, command: str, wait_time: float = 0.5):
    command_to_send = command + "\r"
    readIn = ser.send_command(command)
    time.sleep(wait_time)
    return readIn

#def readAll(ser = serial.Serial):
    #return (ser.read(ser.inWaiting()).decode('utf-8')) 

def pinginate(ser: ConnectHandler, address: str):
    command = "ping " + address + "\r"
    out = ser.send_command(command, read_timeout=60)
    output = ''
    #out = readAll(ser)
    output = output + out
    printL(address + ':\t')
    #check = re.compile('#$')
    
    #while (check.match(out) == None):
        #time.sleep(.1)
        #out = readAll(ser)
        #output = output + out
    
    if ("protocol not running" not in output):
        results = output.split('\n')[2]
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

def logon(ser: ConnectHandler):
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