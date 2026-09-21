# kbuild for the t70-dsa module. DKMS invokes kbuild with M=<this dir>;
# the all/clean targets are for building by hand on the box.
obj-m := t70-dsa.o

KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
