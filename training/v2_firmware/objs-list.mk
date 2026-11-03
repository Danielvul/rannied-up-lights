# Writes objs.in, the linker's object list. Seeed's link rule builds it with $(file ...), which needs
# GNU make 4; macOS ships make 3.81, where $(file ...) silently does nothing and the link fails.
# Run from EPII_CM55M_APP_S:  make -f .../objs-list.mk TARGET=GROVE_VISION_AI_V2 opencorn-objs
include makefile

opencorn-objs:
	@printf "%s\n" $(DEVICE_OBJS) $(INTEGRATE_OBJS) $(HWACCAUTOTEST_OBJS) $(BOARD_OBJS) $(SCENARIO_APP_OBJS) \
	  $(APPL_OBJS) $(TRUSTZONE_SEC_OBJS) $(NSC_OBJS) $(OS_ALLOBJS) $(OS_HAL_ALLOBJS) $(TFM_OBJS) $(INTERFACE_OBJS) > objs.in
