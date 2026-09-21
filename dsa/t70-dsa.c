// SPDX-License-Identifier: GPL-2.0
/*
 * t70-dsa — WatchGuard T70: SMI bus for the Marvell 88E6176 behind enp4s0.
 *
 * The switch's SMI lines hang off the MDC/MDIO pins of the Intel I210
 * (8086:1537, 1000BASE-KX) that Linux knows as enp4s0. igb never uses that
 * MDIO block on a KX-mode device, so this module maps the I210's BAR0 next to
 * igb and drives MDIC itself, then hands mainline mv88e6xxx a
 * dsa_mv88e6xxx_pdata so the switch shows up as DSA user ports lan3..lan7
 * with enp4s0 as conduit.
 *
 * I210 external-PHY mode (Intel I210 datasheet, MDICNFG 0x0E04): with Destination
 * (bit 31) set, the PHY address driven on the wire is MDICNFG.PHY_ADDR
 * [25:21]; MDIC.PHYADD must be 0. MDICNFG resets to 0 on every MAC reset, so
 * it is rewritten on every transaction.
 *
 * Measured facts (docs/hardware.md): switch ID 0x1761, single-chip SMI
 * addressing (sw_addr 0), CPU port 5 (SerDes), user ports 0..4, port 6 unused.
 */
#include <linux/module.h>
#include <linux/pci.h>
#include <linux/netdevice.h>
#include <linux/phy.h>
#include <linux/mdio.h>
#include <linux/io.h>
#include <linux/iopoll.h>
#include <linux/delay.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/platform_data/mv88e6xxx.h>
#include <net/dsa.h>

#define I210_VENDOR		0x8086
#define I210_DEVICE_KX		0x1537

#define E1000_MDIC		0x00020
#define E1000_MDICNFG		0x00E04
#define E1000_SWSM		0x05B50
#define E1000_SW_FW_SYNC	0x05B5C

#define MDIC_OP_WRITE		BIT(26)
#define MDIC_OP_READ		BIT(27)
#define MDIC_READY		BIT(28)
#define MDIC_ERROR		BIT(30)
#define MDICNFG_EXT_MDIO	BIT(31)
#define MDICNFG_PHY_SHIFT	21

#define SWSM_SMBI		BIT(0)
#define SWSM_SWESMBI		BIT(1)
#define SWFW_PHY0_SM		0x02
#define SWFW_FW_SHIFT		16

#define T70_NUM_USER_PORTS	5
#define T70_CPU_PORT		5
#define T70_SWITCH_ID_REG	3
#define T70_PORT0_SMI_ADDR	0x10
#define T70_PRODUCT_88E6176	0x176

/* 88E6176 registers used by the CPU-port fixup (see t70_cpu_port_fixup) */
#define T70_PORT5_SMI_ADDR	0x15
#define T70_PORT_MAC_CTL	0x01
#define T70_PORT_MAC_CTL_FORCED_1000FD	0x003e	/* force link up, full duplex, 1000 */
#define T70_G2_SMI_ADDR		0x1c
#define T70_G2_SMI_PHY_CMD	0x18
#define T70_G2_SMI_PHY_DATA	0x19
#define T70_G2_SMI_PHY_BUSY	BIT(15)
#define T70_G2_SMI_PHY_WRITE22	0x9400	/* busy | clause 22 | op write */
#define T70_SERDES_PHY_ADDR	0x0f
#define T70_SERDES_PAGE_REG	22
#define T70_SERDES_PAGE_FIBER	1
#define T70_SERDES_BMCR_1000FD_NOAN	0x0140	/* powered, autoneg off, 1000 full */

static char *conduit = "enp4s0";
module_param(conduit, charp, 0444);
MODULE_PARM_DESC(conduit, "netdev of the I210 the switch hangs off (default enp4s0)");

static char *port_names = "lan3,lan4,lan5,lan6,lan7";
module_param(port_names, charp, 0444);
MODULE_PARM_DESC(port_names, "names for switch ports 0..4, comma separated");

struct t70 {
	void __iomem *regs;
	struct pci_dev *pdev;
	struct net_device *ndev;
	struct mii_bus *bus;
	struct mdio_device *mdiodev;
	struct mutex lock;		/* serialises MDIC transactions */
	struct dsa_mv88e6xxx_pdata pdata;
	char names[T70_NUM_USER_PORTS][IFNAMSIZ];
};

static struct t70 *t70;
static char t70_cpu_name[] = "cpu";

/* --- I210 software/firmware semaphore (same protocol as igb's i210 code) --- */

static int t70_get_hw_semaphore(struct t70 *t)
{
	u32 swsm;
	int i;

	for (i = 0; i < 1000; i++) {
		if (!(readl(t->regs + E1000_SWSM) & SWSM_SMBI))
			break;
		udelay(50);
	}
	if (i == 1000)
		return -EBUSY;

	for (i = 0; i < 1000; i++) {
		swsm = readl(t->regs + E1000_SWSM);
		writel(swsm | SWSM_SWESMBI, t->regs + E1000_SWSM);
		if (readl(t->regs + E1000_SWSM) & SWSM_SWESMBI)
			return 0;
		udelay(50);
	}
	writel(readl(t->regs + E1000_SWSM) & ~SWSM_SMBI, t->regs + E1000_SWSM);
	return -EBUSY;
}

static void t70_put_hw_semaphore(struct t70 *t)
{
	u32 swsm = readl(t->regs + E1000_SWSM);

	writel(swsm & ~(SWSM_SMBI | SWSM_SWESMBI), t->regs + E1000_SWSM);
}

static int t70_acquire_swfw(struct t70 *t)
{
	u32 mask = SWFW_PHY0_SM, fwmask = SWFW_PHY0_SM << SWFW_FW_SHIFT;
	u32 sync;
	int i;

	for (i = 0; i < 200; i++) {
		if (t70_get_hw_semaphore(t))
			return -EBUSY;
		sync = readl(t->regs + E1000_SW_FW_SYNC);
		if (!(sync & (mask | fwmask))) {
			writel(sync | mask, t->regs + E1000_SW_FW_SYNC);
			t70_put_hw_semaphore(t);
			return 0;
		}
		t70_put_hw_semaphore(t);
		usleep_range(1000, 2000);
	}
	return -ETIMEDOUT;
}

static void t70_release_swfw(struct t70 *t)
{
	u32 sync;

	if (t70_get_hw_semaphore(t))
		return;
	sync = readl(t->regs + E1000_SW_FW_SYNC);
	writel(sync & ~SWFW_PHY0_SM, t->regs + E1000_SW_FW_SYNC);
	t70_put_hw_semaphore(t);
}

/* --- MDIC transactions --- */

static int t70_mdic(struct t70 *t, int addr, int reg, u32 op, u16 data)
{
	u32 v;
	int ret;

	writel(MDICNFG_EXT_MDIO | ((u32)addr << MDICNFG_PHY_SHIFT),
	       t->regs + E1000_MDICNFG);
	writel(op | ((u32)reg << 16) | data, t->regs + E1000_MDIC);
	ret = readl_poll_timeout(t->regs + E1000_MDIC, v, v & MDIC_READY,
				 10, 10000);
	if (ret)
		return ret;
	if (v & MDIC_ERROR)
		return -EIO;
	return v & 0xffff;
}

static int t70_smi_xfer(struct mii_bus *bus, int addr, int reg, u32 op, u16 data)
{
	struct t70 *t = bus->priv;
	bool locked;
	int ret;

	mutex_lock(&t->lock);
	locked = !t70_acquire_swfw(t);
	if (!locked)
		pr_warn_ratelimited("t70-dsa: I210 SW/FW semaphore timeout, proceeding\n");
	ret = t70_mdic(t, addr, reg, op, data);
	if (locked)
		t70_release_swfw(t);
	mutex_unlock(&t->lock);
	return ret;
}

static int t70_smi_read(struct mii_bus *bus, int addr, int reg)
{
	return t70_smi_xfer(bus, addr, reg, MDIC_OP_READ, 0);
}

static int t70_smi_write(struct mii_bus *bus, int addr, int reg, u16 val)
{
	int ret = t70_smi_xfer(bus, addr, reg, MDIC_OP_WRITE, val);

	return ret < 0 ? ret : 0;
}

/* --- CPU port fixup ---
 *
 * With platform data there is no device-tree node for port 5, so DSA skips
 * phylink for it ("skipping link registration for CPU port 5") and mv88e6xxx,
 * which powers and configures a SerDes only through phylink, leaves the
 * 1000BASE-X SerDes in power-down with autoneg on. igb runs the I210 in
 * 1000BASE-KX "parallel detect" mode (no autoneg), so the link never comes up.
 * Do what phylink would do for a fixed-link 1000base-x CPU port: SerDes BMCR
 * powered/no-AN/1000/FD, MAC control forced up at 1000 full. Verified by hand
 * with scripts/t70-mdio-probe.py --cpu-force on 2026-09-20.
 *
 * The SerDes registers are page 1 of SMI-PHY 0xF behind Global2; that is a
 * command/data pair, so the whole sequence runs with the bus lock held to
 * keep it atomic against mv88e6xxx's own Global2 traffic.
 */

static int t70_g2_wait(struct mii_bus *bus)
{
	int i, v;

	for (i = 0; i < 100; i++) {
		v = __mdiobus_read(bus, T70_G2_SMI_ADDR, T70_G2_SMI_PHY_CMD);
		if (v < 0)
			return v;
		if (!(v & T70_G2_SMI_PHY_BUSY))
			return 0;
		usleep_range(100, 200);
	}
	return -ETIMEDOUT;
}

static int t70_g2_phy_write(struct mii_bus *bus, int phy, int reg, u16 val)
{
	int err;

	err = t70_g2_wait(bus);
	if (err)
		return err;
	err = __mdiobus_write(bus, T70_G2_SMI_ADDR, T70_G2_SMI_PHY_DATA, val);
	if (err)
		return err;
	err = __mdiobus_write(bus, T70_G2_SMI_ADDR, T70_G2_SMI_PHY_CMD,
			      T70_G2_SMI_PHY_WRITE22 | (phy << 5) | reg);
	if (err)
		return err;
	return t70_g2_wait(bus);
}

static int t70_cpu_port_fixup(struct t70 *t)
{
	struct mii_bus *bus = t->bus;
	const char *step = "select SerDes page 1";
	int err, err2;

	mutex_lock(&bus->mdio_lock);
	err = t70_g2_phy_write(bus, T70_SERDES_PHY_ADDR, T70_SERDES_PAGE_REG, T70_SERDES_PAGE_FIBER);
	if (!err) {
		step = "SerDes BMCR";
		err = t70_g2_phy_write(bus, T70_SERDES_PHY_ADDR, MII_BMCR, T70_SERDES_BMCR_1000FD_NOAN);
		/* always leave the PHY on page 0, whatever happened to the BMCR write */
		err2 = t70_g2_phy_write(bus, T70_SERDES_PHY_ADDR, T70_SERDES_PAGE_REG, 0);
		if (!err && err2) {
			step = "restore SerDes page 0";
			err = err2;
		}
	}
	if (!err) {
		step = "port 5 MAC control";
		err = __mdiobus_write(bus, T70_PORT5_SMI_ADDR, T70_PORT_MAC_CTL,
				      T70_PORT_MAC_CTL_FORCED_1000FD);
	}
	mutex_unlock(&bus->mdio_lock);
	if (err)
		pr_warn("t70-dsa: CPU port 5 fixup failed at %s: %d\n", step, err);
	return err;
}

/* --- setup / teardown --- */

static int t70_bus_match(struct device *dev, const struct device_driver *drv)
{
	return !strcmp(drv->name, "mv88e6085");
}

static int t70_parse_port_names(struct t70 *t)
{
	char *dup, *s, *tok;
	int i = 0;

	dup = kstrdup(port_names, GFP_KERNEL);
	if (!dup)
		return -ENOMEM;
	s = dup;
	while ((tok = strsep(&s, ",")) != NULL) {
		if (i == T70_NUM_USER_PORTS || !*tok || strlen(tok) >= IFNAMSIZ) {
			i = -1;
			break;
		}
		strscpy(t->names[i], tok, IFNAMSIZ);
		t->pdata.cd.port_names[i] = t->names[i];
		i++;
	}
	kfree(dup);
	if (i != T70_NUM_USER_PORTS) {
		pr_err("t70-dsa: port_names must be exactly %d comma-separated names shorter than %d chars\n",
		       T70_NUM_USER_PORTS, IFNAMSIZ);
		return -EINVAL;
	}
	return 0;
}

static int __init t70_init(void)
{
	struct net_device *ndev;
	struct device *parent;
	struct pci_dev *pdev;
	struct t70 *t;
	int ret, id;

	ndev = dev_get_by_name(&init_net, conduit);
	if (!ndev) {
		pr_err("t70-dsa: conduit %s not found\n", conduit);
		return -ENODEV;
	}
	parent = ndev->dev.parent;
	if (!parent || !dev_is_pci(parent)) {
		pr_err("t70-dsa: %s is not a PCI device\n", conduit);
		ret = -ENODEV;
		goto put_ndev;
	}
	pdev = to_pci_dev(parent);
	if (pdev->vendor != I210_VENDOR || pdev->device != I210_DEVICE_KX) {
		pr_err("t70-dsa: %s is %04x:%04x, expected Intel I210 backplane %04x:%04x\n",
		       conduit, pdev->vendor, pdev->device, I210_VENDOR, I210_DEVICE_KX);
		ret = -ENODEV;
		goto put_ndev;
	}

	t = kzalloc(sizeof(*t), GFP_KERNEL);
	if (!t) {
		ret = -ENOMEM;
		goto put_ndev;
	}
	mutex_init(&t->lock);
	t->ndev = ndev;
	t->pdev = pdev;

	ret = t70_parse_port_names(t);
	if (ret)
		goto free_t;

	/* igb holds the region; we only need a second mapping of the same MMIO */
	t->regs = ioremap(pci_resource_start(pdev, 0), pci_resource_len(pdev, 0));
	if (!t->regs) {
		pr_err("t70-dsa: cannot map BAR0 of %s\n", pci_name(pdev));
		ret = -ENOMEM;
		goto free_t;
	}

	t->bus = mdiobus_alloc();
	if (!t->bus) {
		ret = -ENOMEM;
		goto unmap;
	}
	t->bus->name = "t70-smi";
	snprintf(t->bus->id, MII_BUS_ID_SIZE, "t70-smi");
	t->bus->read = t70_smi_read;
	t->bus->write = t70_smi_write;
	t->bus->priv = t;
	t->bus->parent = &pdev->dev;
	t->bus->phy_mask = ~0u;		/* no PHY autoprobe: the PHYs belong to the switch */

	ret = mdiobus_register(t->bus);
	if (ret) {
		pr_err("t70-dsa: mdiobus_register failed: %d\n", ret);
		goto free_bus;
	}

	id = mdiobus_read(t->bus, T70_PORT0_SMI_ADDR, T70_SWITCH_ID_REG);
	if (id < 0 || (id >> 4) != T70_PRODUCT_88E6176) {
		if (id < 0)
			pr_err("t70-dsa: switch ID read failed: %d\n", id);
		else
			pr_err("t70-dsa: switch ID 0x%04x, expected 88E6176 (0x176x); refusing to drive it\n", id);
		ret = -ENODEV;
		goto unreg_bus;
	}
	pr_info("t70-dsa: 88E6176 rev %d on %s (%s)\n", id & 0xf, conduit, pci_name(pdev));

	t->pdata.compatible = "marvell,mv88e6085";	/* family entry; chip auto-detected */
	t->pdata.enabled_ports = GENMASK(T70_CPU_PORT, 0);
	t->pdata.netdev = ndev;
	t->pdata.cd.sw_addr = 0;
	t->pdata.cd.port_names[T70_CPU_PORT] = t70_cpu_name;
	t->pdata.cd.netdev[T70_CPU_PORT] = &ndev->dev;

	t->mdiodev = mdio_device_create(t->bus, 0);
	if (IS_ERR(t->mdiodev)) {
		ret = PTR_ERR(t->mdiodev);
		pr_err("t70-dsa: mdio_device_create failed: %d\n", ret);
		goto unreg_bus;
	}
	/* No modalias field on this kernel: name the driver explicitly (generic
	 * driver_override) and also match it ourselves via the per-device hook.
	 */
	t->mdiodev->bus_match = t70_bus_match;
	ret = device_set_driver_override(&t->mdiodev->dev, "mv88e6085");
	if (ret) {
		pr_err("t70-dsa: device_set_driver_override failed: %d\n", ret);
		goto free_mdiodev;
	}
	t->mdiodev->dev.platform_data = &t->pdata;

	ret = mdio_device_register(t->mdiodev);
	if (ret) {
		pr_err("t70-dsa: mdio_device_register failed: %d\n", ret);
		goto free_mdiodev;
	}

	/* mv88e6xxx has probed synchronously by now (and soft-reset the chip) */
	ret = t70_cpu_port_fixup(t);
	if (ret)
		pr_warn("t70-dsa: enp4s0 will stay without carrier\n");
	else
		pr_info("t70-dsa: CPU port 5 SerDes forced up (1000BASE-X, no autoneg)\n");

	t70 = t;
	return 0;

free_mdiodev:
	device_set_driver_override(&t->mdiodev->dev, NULL);	/* frees the override string */
	mdio_device_free(t->mdiodev);
unreg_bus:
	mdiobus_unregister(t->bus);
free_bus:
	mdiobus_free(t->bus);
unmap:
	iounmap(t->regs);
free_t:
	kfree(t);
put_ndev:
	dev_put(ndev);
	return ret;
}

static void __exit t70_exit(void)
{
	struct t70 *t = t70;

	mdio_device_remove(t->mdiodev);
	device_set_driver_override(&t->mdiodev->dev, NULL);
	mdio_device_free(t->mdiodev);
	mdiobus_unregister(t->bus);
	mdiobus_free(t->bus);
	iounmap(t->regs);
	dev_put(t->ndev);
	kfree(t);
	t70 = NULL;
}

module_init(t70_init);
module_exit(t70_exit);

MODULE_AUTHOR("Ryan Tober");
MODULE_DESCRIPTION("WatchGuard T70: Marvell 88E6176 SMI over the I210 MDIC block, with mv88e6xxx platform data");
MODULE_LICENSE("GPL");
MODULE_SOFTDEP("pre: igb mv88e6xxx tag_dsa");
