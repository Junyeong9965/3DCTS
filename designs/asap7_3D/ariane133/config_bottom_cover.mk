# config_bottom_cover.mk — CTS step: bottom=CLASS COVER
# Auto-generated for V53-FM-BUF-HCAL-MACRO
include $(dir $(lastword $(MAKEFILE_LIST)))config.mk

# Override SC_LEF: bottom=COVER (CTS cannot place bottom buffers)
# Use deferred = (not :=) so PLATFORM_DIR resolves at use time
export SC_LEF = $(SC_LEF_BOTTOM_COVER)

# Override ADDITIONAL_LEFS: bottom fakeram=COVER
export ADDITIONAL_LEFS = $(ADDITIONAL_LEFS_BOTTOM_COVER)
