# Only the diagnostic target receives the GPIO observer.
function(add_grbl_gpio_observer)
    target_sources(grblHAL_sim PRIVATE "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/simulator-trace.c")
    target_link_options(grblHAL_sim PRIVATE "-Wl,--wrap=mcu_gpio_set")
endfunction()
cmake_language(DEFER CALL add_grbl_gpio_observer)
