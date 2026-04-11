#!/bin/bash

. /usr/lib/tuned/functions

start() {
    enable_usb_autosuspend
    enable_wifi_powersave
    return 0
}

stop() {
    disable_usb_autosuspend
    disable_wifi_powersave
    return 0
}

process $@
