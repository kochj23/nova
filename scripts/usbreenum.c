// usbreenum - macOS equivalent of Linux `usbreset`: force a USB device to
// re-enumerate by VID/PID, without unplugging it.
//
// *** WARNING, measured on .77 2026-10-21 ***
// On a device that is enumerated but whose FIRMWARE is already wedged, this
// returns kIOReturnSuccess and the device then does NOT come back on the bus
// at all -- it goes from "present but deaf" to "absent", and still needs a
// physical replug. Only use it on a device that is still talking (e.g. to
// clear a stalled SDRplay bulk pipe), never as a hail-mary on a dead one.
//
// Why it exists: the SDRplay API and the Meshtastic serial link both wedge in
// ways Linux recovers with `usbreset` (see agent_docs sdr-fleet); macOS has no
// such tool.
//
// Build:  cc -O2 -o usbreenum usbreenum.c -framework IOKit -framework CoreFoundation
// Run:    sudo ./usbreenum <vid_hex> <pid_hex>     e.g. sudo ./usbreenum 1df7 3020
#include <stdio.h>
#include <stdlib.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/usb/IOUSBLib.h>
#include <CoreFoundation/CoreFoundation.h>

int main(int argc, char **argv) {
    if (argc != 3) { fprintf(stderr, "usage: usbreenum <vid_hex> <pid_hex>\n"); return 2; }
    long vid = strtol(argv[1], NULL, 16), pid = strtol(argv[2], NULL, 16);

    CFMutableDictionaryRef match = IOServiceMatching(kIOUSBDeviceClassName);
    if (!match) { fprintf(stderr, "IOServiceMatching failed\n"); return 1; }
    int v = (int)vid, p = (int)pid;
    CFNumberRef cv = CFNumberCreate(NULL, kCFNumberIntType, &v);
    CFNumberRef cp = CFNumberCreate(NULL, kCFNumberIntType, &p);
    CFDictionarySetValue(match, CFSTR(kUSBVendorID), cv);
    CFDictionarySetValue(match, CFSTR(kUSBProductID), cp);
    CFRelease(cv); CFRelease(cp);

    io_iterator_t it = 0;
    if (IOServiceGetMatchingServices(kIOMainPortDefault, match, &it) != KERN_SUCCESS) {
        fprintf(stderr, "IOServiceGetMatchingServices failed\n"); return 1;
    }

    int found = 0, reset = 0;
    io_service_t dev;
    while ((dev = IOIteratorNext(it))) {
        found++;
        IOCFPlugInInterface **plugin = NULL;
        SInt32 score = 0;
        if (IOCreatePlugInInterfaceForService(dev, kIOUSBDeviceUserClientTypeID,
                                             kIOCFPlugInInterfaceID, &plugin, &score) == KERN_SUCCESS && plugin) {
            IOUSBDeviceInterface **usb = NULL;
            if ((*plugin)->QueryInterface(plugin, CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID),
                                          (LPVOID *)&usb) == S_OK && usb) {
                // Opening exclusively usually fails when a kext/dext owns the
                // device; USBDeviceReEnumerate works anyway on an unopened handle.
                (*usb)->USBDeviceOpen(usb);
                IOReturn r = (*usb)->USBDeviceReEnumerate(usb, 0);
                printf("device %04lx:%04lx re-enumerate -> 0x%08x (%s)\n", vid, pid, r,
                       r == kIOReturnSuccess ? "ok" : "failed");
                if (r == kIOReturnSuccess) reset++;
                (*usb)->USBDeviceClose(usb);
                (*usb)->Release(usb);
            }
            IODestroyPlugInInterface(plugin);
        }
        IOObjectRelease(dev);
    }
    IOObjectRelease(it);

    if (!found) { fprintf(stderr, "no device %04lx:%04lx in the IORegistry\n", vid, pid); return 3; }
    return reset ? 0 : 1;
}
