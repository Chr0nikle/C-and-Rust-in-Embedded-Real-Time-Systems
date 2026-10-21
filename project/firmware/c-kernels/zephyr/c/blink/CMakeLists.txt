cmake_minimum_required(VERSION 3.20)
project(zephyr_blink)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})

target_sources(app PRIVATE main.c)
