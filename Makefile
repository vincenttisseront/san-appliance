# Targets for the Ubuntu 24.04 live ISO. The 26.04 image stays in image/.
.PHONY: doctor iso test-unit test-iscsi test-features test-boot test-perf

doctor:
	live/doctor.sh

iso:
	sudo live/build-iso.sh

test-unit:
	PYTHONPATH=live24/tui python3 -m unittest discover -s live24/tui/tests -v

test-iscsi:
	sudo live/iscsi-test.sh

test-features:
	sudo live/feature-test.sh

test-boot:
	sudo live/boot-matrix.sh

test-perf:
	sudo live/perf-test.sh
