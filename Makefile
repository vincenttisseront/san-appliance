# Targets for the Ubuntu 24.04 live ISO. The 26.04 image stays in image/.
.PHONY: doctor iso

doctor:
	live/doctor.sh

iso:
	sudo live/build-iso.sh
