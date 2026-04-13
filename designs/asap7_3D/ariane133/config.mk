export DESIGN_NAME = ariane
export DESIGN_NICKNAME = ariane133
export PLATFORM    = asap7_3D

export SC_LEF_UPPER_COVER ?= \
  $(PLATFORM_DIR)/lef_bottom/asap7sc7p5t_28_R_1x_220121a.bottom.lef \
  $(PLATFORM_DIR)/lef_upper/asap7sc7p5t_28_R_1x_220121a.upper.cover.lef
export SC_LEF_BOTTOM_COVER ?= \
  $(PLATFORM_DIR)/lef_bottom/asap7sc7p5t_28_R_1x_220121a.bottom.cover.lef \
  $(PLATFORM_DIR)/lef_upper/asap7sc7p5t_28_R_1x_220121a.upper.lef

export ADDITIONAL_LEFS = $(sort $(wildcard $(PLATFORM_DIR)/lef_bottom/fakeram_block/*.lef)) \
                         $(sort $(wildcard $(PLATFORM_DIR)/lef_upper/fakeram_block/*.lef))
export ADDITIONAL_LEFS_UPPER_COVER = $(sort $(wildcard $(PLATFORM_DIR)/lef_bottom/fakeram_block/*.lef)) \
                                     $(sort $(wildcard $(PLATFORM_DIR)/lef_upper/fakeram_cover/*.lef))
export ADDITIONAL_LEFS_BOTTOM_COVER = $(sort $(wildcard $(PLATFORM_DIR)/lef_bottom/fakeram_cover/*.lef)) \
                                      $(sort $(wildcard $(PLATFORM_DIR)/lef_upper/fakeram_block/*.lef))
export ADDITIONAL_LIBS = $(sort $(wildcard $(PLATFORM_DIR)/lib_bottom/NLDM/fakeram/*.lib)) \
                         $(sort $(wildcard $(PLATFORM_DIR)/lib_upper/NLDM/fakeram/*.lib))

export CORE_UTILIZATION = 40
export CORE_ASPECT_RATIO = 1
export CORE_MARGIN = 5
# GPL-0306 fix: 133 macros reduce usable area, auto-density too high
export PLACE_DENSITY = 0.20
export PLACE_DENSITY_LB_ADDON = 0.08
export TNS_END_PERCENT        = 100
export DETAILED_ROUTE_END_ITERATION = 10
export GLOBAL_ROUTE_ARGS = -verbose -congestion_iterations 30

# GPL-0306 fix: 0.2 too small for 133 macros, increased to 1.0
#export MACRO_PLACE_HALO = 0.2 0.2
export MACRO_PLACE_HALO = 1.0 1.0

export NUM_CORES   ?= 32
