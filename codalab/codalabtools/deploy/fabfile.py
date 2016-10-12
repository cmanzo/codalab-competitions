"""
Defines deployment commands.
"""

import datetime
import logging
import logging.config
import os
from os.path import (abspath, dirname)
import sys

# Add codalabtools to the module search path
sys.path.append(dirname(dirname(dirname(abspath(__file__)))))

from StringIO import StringIO
from fabric.api import (cd,
                        env,
                        execute,
                        get,
                        prefix,
                        put,
                        require,
                        task,
                        roles,
                        require,
                        run,
                        settings,
                        shell_env,
                        sudo)
from fabric.contrib.files import exists
from fabric.network import ssh
from fabric.utils import fastprint
from codalabtools.deploy import DeploymentConfig, Deployment

logger = logging.getLogger('codalabtools')


############################################################
# Configuration (run every time)

def provision_packages(packages=None):
    """
    Installs a set of packages on a host machine. Web servers and compute workers.

    packages: A string listing the packages which will get installed with the command:
        sudo apt-get -y install <packages>
    """
    sudo('apt-get update')
    sudo('apt-get upgrade')
    sudo('apt-get install -y python-pip')
    sudo('apt-get -y install %s' % packages)
    sudo('apt-get -y install git')
    sudo('apt-get -y install python-virtualenv')
    sudo('apt-get -y install virtualenvwrapper')
    sudo('apt-get -y install python-setuptools')
    sudo('apt-get -y install build-essential')
    sudo('apt-get -y install python-dev')


@task
def provision_web_packages():
    """
    Installs required software packages on a newly provisioned web instance.
    """
    packages = ('libjpeg-dev nginx supervisor xclip  zip' +
                'libmysqlclient-dev uwsgi-plugin-python')
    provision_packages(packages)


@task
def provision_compute_workers_packages():
    """
    Installs required software packages on a newly provisioned compute worker machine.
    """
    packages = ('python-crypto libpcre3-dev libpng12-dev libjpeg-dev libmysqlclient-dev uwsgi-plugin-python')
    provision_packages(packages)


@task
def using(path):
    """
    Specifies a location for the CodaLab configuration file (e.g., deployment.config)
    """
    env.cfg_path = path


@task
def config(label):
    """
    Reads deployment parameters for the given setup.
    label: Label identifying the desired setup (e.g., prod, test, etc.)
    """
    env.cfg_label = label
    print "Deployment label is:", env.cfg_label
    print "Loading configuration from:", env.cfg_path
    configuration = DeploymentConfig(label, env.cfg_path)
    print "Configuring logger..."
    logging.config.dictConfig(configuration.getLoggerDictConfig())
    logger.info("Loaded configuration from file: %s", configuration.getFilename())
    env.roledefs = {'web': configuration.getWebHostnames()}

    # Credentials
    env.user = configuration.getVirtualMachineLogonUsername()
    # COMMENT THIS OUT LATER, USED ONLY FOR OLD DEPLOYMENT
    # env.password = configuration.getVirtualMachineLogonPassword()
    # env.key_filename = configuration.getServiceCertificateKeyFilename()

    # Repository
    env.git_codalab_tag = configuration.getGitTag()
    env.deploy_codalab_dir = 'codalab-competitions'  # Directory for codalab competitions

    env.django_settings_module = 'codalab.settings'
    env.django_configuration = configuration.getDjangoConfiguration()  # Prod or Dev
    env.config_http_port = '80'
    env.config_server_name = "{0}.cloudapp.net".format(configuration.getServiceName())
    print "Deployment configuration is for:", env.config_server_name

    env.configuration = True
    env.SHELL_ENV = {}


def setup_env():
    env.SHELL_ENV.update(dict(
        DJANGO_SETTINGS_MODULE=env.django_settings_module,
        DJANGO_CONFIGURATION=env.django_configuration,
        CONFIG_HTTP_PORT=env.config_http_port,
        CONFIG_SERVER_NAME=env.config_server_name,
    ))
    return prefix('source ~/%s/venv/bin/activate' % env.deploy_codalab_dir), shell_env(**env.SHELL_ENV)


############################################################
# Installation (one-time)


@roles('web')
@task
def install_web():
    '''
    Install everything from scratch (idempotent).
    '''
    # Install Linux packages
    provision_web_packages()

    # Setup repositories
    def ensure_repo_exists(repo, dest):
        run('[ -e %s ] || git clone %s %s' % (dest, repo, dest))
    ensure_repo_exists('https://github.com/codalab/codalab-competitions', env.deploy_codalab_dir)

    # Initial setup
    with cd(env.deploy_codalab_dir):
        run('git checkout %s' % env.git_codalab_tag)
        run('./dev_setup.sh')

    # Install mysql database
    install_mysql()
    # Deploy!
    _deploy()
    nginx_restart()
    supervisor('stop')
    supervisor('start')


@task
def provision_compute_worker(label):
    '''
    Install compute workers from scracth. Run only once
    '''
    # Install packages
    provision_compute_workers_packages()
    env.deploy_codalab_dir = 'codalab-competitions'

    # Setup repositories
    def ensure_repo_exists(repo, dest):
        run('[ -e %s ] || git clone %s %s' % (dest, repo, dest))
    ensure_repo_exists('https://github.com/codalab/codalab-competitions', env.deploy_codalab_dir)
    deploy_compute_workers(label=label)


@task
def deploy_compute_workers(label):
    '''
    Deploy/update compute workers.
    For monitoring make sure the azure instance has the port 8000 forwarded

    :param label: Either test or prod
    '''
    env.deploy_codalab_dir = 'codalab-competitions'
    # Create .codalabconfig within home directory
    env.label = label
    cfg = DeploymentConfig(env.label, env.cfg_path)
    dep = Deployment(cfg)
    buf = StringIO()
    buf.write(dep.get_compute_workers_file_content())
    settings_file = os.path.join('~', '.codalabconfig')
    put(buf, settings_file)
    env.git_codalab_tag = cfg.getGitTag()

    # Initial setup
    with cd(env.deploy_codalab_dir):
        run('git checkout %s' % env.git_codalab_tag)
        run('git pull')
        run('./dev_setup.sh')

    # Write the worker configuration file

    # password = os.environ.get('CODALAB_COMPUTE_MONITOR_PASSWORD', None)
    # assert password, "CODALAB_COMPUTE_MONITOR_PASSWORD environment variable required to setup compute workers!"

    # run("source /home/azureuser/codalab-competitions/venv/bin/activate && pip install bottle==0.12.8")

    put(
        local_path='/Users/flaviozhingri/work/codalab/codalab/codalabtools/deploy/configs/upstart/codalab-compute-worker.conf',
        remote_path='/etc/init/codalab-compute-worker.conf',
        use_sudo=True
    )
    # put(
    #     local_path='configs/upstart/codalab-monitor.conf',
    #     remote_path='/etc/init/codalab-monitor.conf',
    #     use_sudo=True
    # )
    # run("echo %s > /home/azureuser/codalab/codalab/codalabtools/compute/password.txt" % password)

    with settings(warn_only=True):
        sudo("stop codalab-compute-worker")
        # sudo("stop codalab-monitor")
        sudo("start codalab-compute-worker")
        # sudo("start codalab-monitor")


@roles('web')
@task
def install_mysql(choice='all'):
    """
    Installs a local instance of MySQL of the web instance. This will only work
    if the number of web instances is one.

    choice: Indicates which assets to create/install:
        'mysql'      -> just install MySQL; don't create the databases
        'website_db' -> just create the website database
        'all' or ''  -> install everything
    """
    require('configuration')
    if len(env.roledefs['web']) != 1:
        raise Exception("Task install_mysql requires exactly one web instance.")

    if choice == 'mysql':
        choices = {'mysql'}
    elif choice == 'website_db':
        choices = {'website_db'}
    elif choice == 'all':
        choices = {'mysql', 'website_db'}
    else:
        raise ValueError("Invalid choice: %s. Valid choices are: 'build', 'web' or 'all'." % (choice))

    configuration = DeploymentConfig(env.cfg_label, env.cfg_path)
    dba_password = configuration.getDatabaseAdminPassword()

    if 'mysql' in choices:
        sudo('DEBIAN_FRONTEND=noninteractive apt-get install -y mysql-server')
        sudo('mysqladmin -u root password {0}'.format(dba_password))

    if 'website_db' in choices:
        db_name = configuration.getDatabaseName()
        db_user = configuration.getDatabaseUser()
        db_password = configuration.getDatabasePassword()
        cmds = ["create database {0};".format(db_name),
                "create user '{0}'@'localhost' IDENTIFIED BY '{1}';".format(db_user, db_password),
                "GRANT ALL PRIVILEGES ON {0}.* TO '{1}'@'localhost' WITH GRANT OPTION;".format(db_name, db_user)]
        run('mysql --user=root --password={0} --execute="{1}"'.format(dba_password, " ".join(cmds)))


############################################################
# Deployment

@roles('web')
@task
def supervisor(command):
    """
    Starts the supervisor on the web instances.
    """
    env_prefix, env_shell = setup_env()
    with env_prefix, env_shell, cd(env.deploy_codalab_dir):
        if command == 'start':
            run('mkdir -p ~/logs')
            run('supervisord -c codalab/config/generated/supervisor.conf')
        elif command == 'stop':
            run('supervisorctl -c codalab/config/generated/supervisor.conf stop all')
            run('supervisorctl -c codalab/config/generated/supervisor.conf shutdown')
            # HACK: since competition worker is multithreaded, we need to kill all running processes
            with settings(warn_only=True):
                run('pkill -9 -f worker.py')
        elif command == 'restart':
            run('supervisorctl -c codalab/config/generated/supervisor.conf restart all')
        else:
            raise 'Unknown command: %s' % command


@roles('web')
@task
def nginx_restart():
    """
    Restarts nginx on the web server.
    """
    sudo('/etc/init.d/nginx restart')


# Maintenance and diagnostics
@roles('web')
@task
def maintenance(mode):
    """
    Begin or end maintenance (mode is 'begin' or 'end')
    """
    modes = {'begin': '1', 'end': '0'}
    if mode not in modes:
        print "Invalid mode. Valid values are 'begin' or 'end'"
        sys.exit(1)

    require('configuration')
    env.SHELL_ENV['MAINTENANCE_MODE'] = modes[mode]

    # Update nginx.conf
    env_prefix, env_shell = setup_env()
    with env_prefix, env_shell, cd(env.deploy_codalab_dir), cd('codalab'):
        run('python manage.py config_gen')

    nginx_restart()


@roles('web')
@task
def deploy():
    """
    Put a maintenance message, deploy, and then restore website.
    """
    maintenance('begin')
    supervisor('stop')
    _deploy()
    supervisor('start')
    maintenance('end')


def _deploy():
    # Update competition website
    # Pull branch and run requirements file, for info about requirments look into dev_setp.sh
    env_prefix, env_shell = setup_env()
    with env_prefix, env_shell, cd(env.deploy_codalab_dir):
        run('git pull')
        run('git checkout %s' % env.git_codalab_tag)
        run('./dev_setup.sh')

    # Create local.py
    cfg = DeploymentConfig(env.cfg_label, env.cfg_path)
    dep = Deployment(cfg)
    buf = StringIO()
    buf.write(dep.getSettingsFileContent())
    # local.py is generated here. For more info about content look into deploy/__.init__.py
    settings_file = os.path.join(env.deploy_codalab_dir, 'codalab', 'codalab', 'settings', 'local.py')
    put(buf, settings_file)

    # Update the website configuration
    env_prefix, env_shell = setup_env()
    with env_prefix, env_shell, cd(env.deploy_codalab_dir), cd('codalab'):
        # Generate configuration files (bundle_server_config, nginx, etc.)
        # For more info look into https://github.com/greyside/django-config-gen
        run('python manage.py config_gen')
        # Migrate database
        run('python manage.py syncdb --migrate')
        # Create static pages
        run('python manage.py collectstatic --noinput')
        # For sending email, have the right domain name.
        run('python manage.py set_site %s' % cfg.getSslRewriteHosts()[0])
        # Put nginx and supervisor configuration files in place, ln creates symbolic links
        sudo('ln -sf `pwd`/config/generated/nginx.conf /etc/nginx/sites-enabled/codalab.conf')
        sudo('ln -sf `pwd`/config/generated/supervisor.conf /etc/supervisor/conf.d/codalab.conf')
        # Setup new relic
        run('newrelic-admin generate-config %s newrelic.ini' % cfg.getNewRelicKey())

    # Install SSL certficates (/etc/ssl/certs/)
    require('configuration')
    if (len(cfg.getSslCertificateInstalledPath()) > 0) and (len(cfg.getSslCertificateKeyInstalledPath()) > 0):
        put(cfg.getSslCertificatePath(), cfg.getSslCertificateInstalledPath(), use_sudo=True)
        put(cfg.getSslCertificateKeyPath(), cfg.getSslCertificateKeyInstalledPath(), use_sudo=True)
    else:
        logger.info("Skipping certificate installation because both files are not specified.")


# UTILITIES FAB COMMAND
# MOSTLY USED ON COMPUTE WORKERS

@task
def setup_compute_worker_user():
    # Steps to setup compute worker:
    #   1) setup_compute_worker_user (only run this once as it creates a user and will probably fail if re-run)
    #   2) setup_compute_worker_permissions
    #   3) setup_compute_worker_and_monitoring
    sudo('adduser --quiet --disabled-password --gecos "" workeruser')
    sudo('echo workeruser:password | chpasswd')


@task
def install_anaconda_library():
    '''
    Download anaconda package to compute workers.
    '''
    with cd('/home/azureuser'):
        # sudo("wget http://repo.continuum.io/archive/Anaconda2-2.4.0-Linux-x86_64.sh")
        sudo("yes Y anaconda | bash Anaconda2-2.4.0-Linux-x86_64.sh")


@task
def install_coco_api():
    '''
    Install coco api
    '''
    run("which python")
    with shell_env(PATH='/home/azureuser/anaconda/bin'):
        sudo("conda install cython")
    with cd('/home/azureuser'):
        sudo('git clone https://github.com/pdollar/coco.git')
        with shell_env(PATH='/home/azureuser/anaconda/bin'):
            with cd('coco/PythonAPI'):
                run('python setup.py build_ext install')


@task
def set_permissions_on_codalab_temp():
    '''
    Set proper permissions on compute workers.
    '''
    sudo("bindfs -o perms=0777 /codalabtemp /codalabtemp")


@task
def update_compute_worker():
    run('cd codalab && git pull --rebase')
    sudo("stop codalab-compute-worker")
    sudo("stop codalab-monitor")
    sudo("start codalab-compute-worker")
    sudo("start codalab-monitor")


@task
def update_conda():
    with settings(warn_only=True):
        if not run('conda'):
            # If we can't run conda add it to the path
            run('echo "export PATH=~/anaconda/bin:$PATH" >> ~/.bashrc')
    run('conda update --yes --prefix /home/azureuser/anaconda anaconda')


@task
def setup_compute_worker_permissions():
    # Make the /codalabtemp/ files readable
    sudo("apt-get install bindfs")
    sudo("mkdir /codalabtemp")
    sudo("bindfs -o perms=0777 /codalabtemp /codalabtemp")

    # Make private stuff private
    sudo("chown -R azureuser:azureuser ~/codalab-competitions")
    sudo("chmod -R 700 ~/codalab-competitions")
    sudo("chown azureuser:azureuser ~/.codalabconfig")
    sudo("chmod 700 ~/.codalabconfig")


# OTHER UTILITIES SUCH AS MYSQL DUMP, etc
@roles('web')
@task
def get_database_dump():
    '''Saves backups to $CODALAB_MYSQL_BACKUP_DIR/launchdump-year-month-day-hour-min-second.sql.gz'''
    require('configuration')
    configuration = DeploymentConfig(env.cfg_label, env.cfg_path)
    db_host = "localhost"
    db_name = configuration.getDatabaseName()
    db_user = configuration.getDatabaseUser()
    db_password = configuration.getDatabasePassword()

    dump_file_name = 'competitiondump-%s.sql.gz' % datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')

    run('mysqldump --host=%s --user=%s --password=%s %s --port=3306 | gzip > /tmp/%s' % (
        db_host,
        db_user,
        db_password,
        db_name,
        dump_file_name)
    )

    backup_dir = os.environ.get("CODALAB_MYSQL_BACKUP_DIR", "")
    get('/tmp/%s' % dump_file_name, backup_dir)


@task
def install_packages_compute_workers():
    # --yes and --force-yes accepts the Y/N question when installing the package
    sudo('apt-get update')
    sudo('apt-get --yes --force-yes install libsm6 openjdk-7-jre')
    sudo('apt-get --yes --force-yes install r-base')
    sudo('apt-get --yes --force-yes --fix-missing install mono-runtime libmono-system-web-extensions4.0-cil libmono-system-io-compression4.0-cil')

    # check for khiops dir if not, put
    if not exists("/home/azureuser/khiops/"):
        run('mkdir -p /home/azureuser/khiops/')
        put("~/khiops/", "/home/azureuser/") # actually ends up in /home/azureuser/khiops
        sudo("chmod +x /home/azureuser/khiops/bin/64/MODL")


@task
def khiops_print_machine_name_and_id():
    sudo("chmod +x /home/azureuser/khiops/bin/64/MODL")
    sudo("chmod +x /home/azureuser/khiops/get_license_info.sh")
    with cd('/home/azureuser/khiops/'):
        run("./get_license_info.sh")

