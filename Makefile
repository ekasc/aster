SHELL := /bin/bash
ROOT := $(CURDIR)
DEVICE ?= 00008150-001C056A3693401C

.PHONY: all guest host app device project qemu-tci

all: host app

guest:
	./tools/build_guest.sh

host: guest
	./build/host/aster_tests build/guest

project:
	python3 tools/gen_xcodeproj.py

app: guest project
	xcodebuild -project app/Aster.xcodeproj -scheme Aster -configuration Debug \
	  -destination 'id=$(DEVICE)' -derivedDataPath build/xcode \
	  -allowProvisioningUpdates build

device: app
	./tools/run_on_device.sh

qemu-tci:
	./tools/build_qemu_host_tci.sh
