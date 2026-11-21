from glob import glob

from setuptools import setup

package_name = "workcell_sim_ros"

setup(
    name=package_name,
    version="0.1.0",
    # The simulator itself is installed from the repo's src/ so the VM needs only this workspace.
    packages=[package_name, "workcell_sim"],
    package_dir={"workcell_sim": "../../src/workcell_sim"},
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", glob("launch/*.py")),
        ("share/" + package_name + "/rviz", glob("rviz/*.rviz")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Mihindu Paul",
    maintainer_email="mihindu@mtechlanka.com",
    description="ROS2 bridge for the workcell_sim hardware simulator",
    license="MIT",
    entry_points={"console_scripts": ["sim_node = workcell_sim_ros.sim_node:main"]},
)
