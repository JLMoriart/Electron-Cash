# -*- mode: python3 -*-
import datetime
import os
import random
import sys
import tempfile
import threading
import traceback
import weakref

from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *


from electroncash import keystore, Wallet, WalletStorage
from electroncash.network import Network
from electroncash.util import UserCancelled, InvalidPassword, finalization_print_error, TimeoutException, get_new_wallet_name
from electroncash.base_wizard import BaseWizard
from electroncash.i18n import _
from electroncash.wallet import Standard_Wallet

from .seed_dialog import SeedLayout, KeysLayout
from .network_dialog import NetworkChoiceLayout
from .util import *
from .password_dialog import PasswordLayout, PW_NEW
from .bip38_importer import Bip38Importer


class GoBack(Exception):
    pass


MSG_GENERATING_WAIT = _("Electron Cash is generating your addresses, please wait...")
MSG_ENTER_ANYTHING = _("Please enter a seed phrase, a master key, a list of "
                       "Bitcoin addresses, or a list of private keys")
MSG_ENTER_SEED_OR_MPK = _("Please enter a seed phrase or a master key (xpub or xprv):")
MSG_COSIGNER = _("Please enter the master public key of cosigner #{}:")
MSG_ENTER_PASSWORD = _("Choose a password to encrypt your wallet keys.") + '\n'\
                     + _("Leave this field empty if you want to disable encryption.")
MSG_RESTORE_PASSPHRASE = \
    _("Please enter your seed derivation passphrase. "
      "Note: this is NOT your encryption password. "
      "Leave this field empty if you did not use one or are unsure.")


class CosignWidget(QWidget):
    size = 120

    def __init__(self, m, n):
        QWidget.__init__(self)
        self.R = QRect(0, 0, self.size, self.size)
        self.setGeometry(self.R)
        self.setMinimumHeight(self.size)
        self.setMaximumHeight(self.size)
        self.m = m
        self.n = n

    def set_n(self, n):
        self.n = n
        self.update()

    def set_m(self, m):
        self.m = m
        self.update()

    def paintEvent(self, event):
        bgcolor = self.palette().color(QPalette.Background)
        pen = QPen(bgcolor, 7, Qt.SolidLine)
        qp = QPainter()
        qp.begin(self)
        qp.setPen(pen)
        qp.setRenderHint(QPainter.Antialiasing)
        qp.setBrush(Qt.gray)
        for i in range(self.n):
            alpha = int(16* 360 * i/self.n)
            alpha2 = int(16* 360 * 1/self.n)
            qp.setBrush(Qt.green if i<self.m else Qt.gray)
            qp.drawPie(self.R, alpha, alpha2)
        qp.end()


def wizard_dialog(func):
    def func_wrapper(*args, **kwargs):
        run_next = kwargs['run_next']
        wizard = args[0]
        wizard.back_button.setText(_('Back') if wizard.can_go_back() else _('Cancel'))
        try:
            out = func(*args, **kwargs)
        except GoBack:
            wizard.go_back() if wizard.can_go_back() else wizard.close()
            return
        except UserCancelled:
            return

        if type(out) is not tuple:
            out = (out,)
        run_next(*out)
    return func_wrapper


# WindowModalDialog must come first as it overrides show_error
class InstallWizard(QDialog, MessageBoxMixin, BaseWizard):

    accept_signal = pyqtSignal()
    synchronized_signal = pyqtSignal(str)

    def __init__(self, config, app, plugins, storage):
        BaseWizard.__init__(self, config, storage)
        QDialog.__init__(self, None)
        self.setWindowTitle('Electron Cash  -  ' + _('Startup Wizard'))
        self.app = app
        self.config = config
        # Set for base base class
        self.plugins = plugins
        self.setMinimumSize(900, 600)
        self.accept_signal.connect(self.accept)
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 14px;")
        self.main_widget = QWidget()
        self.back_button = QPushButton(_("Back"), self)
        self.back_button.setText(_('Back') if self.can_go_back() else _('Cancel'))
        self.next_button = QPushButton(_("Next"), self)
        self.next_button.setDefault(True)
        btn_style = "font-size: 24px; padding: 8px 24px;"
        self.back_button.setStyleSheet(btn_style)
        self.next_button.setStyleSheet(btn_style)
        self.logo = QLabel()
        self.please_wait = QLabel(_("Please wait..."))
        self.please_wait.setAlignment(Qt.AlignCenter)
        self.icon_filename = None
        self.loop = QEventLoop()
        self.rejected.connect(lambda: self.loop.exit(0))
        self.back_button.clicked.connect(lambda: self.loop.exit(1))
        self.next_button.clicked.connect(lambda: self.loop.exit(2))
        outer_vbox = QVBoxLayout(self)
        inner_vbox = QVBoxLayout()
        inner_vbox.addWidget(self.title)
        inner_vbox.addWidget(self.main_widget, 1)  # stretch factor so content fills space
        inner_vbox.addWidget(self.please_wait)
        scroll_widget = QWidget()
        scroll_widget.setLayout(inner_vbox)
        scroll = QScrollArea()
        scroll.setWidget(scroll_widget)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidgetResizable(True)
        icon_vbox = QVBoxLayout()
        icon_vbox.addWidget(self.logo)
        icon_vbox.addStretch(1)
        hbox = QHBoxLayout()
        hbox.addLayout(icon_vbox)
        hbox.addSpacing(5)
        hbox.addWidget(scroll)
        hbox.setStretchFactor(scroll, 1)
        outer_vbox.addLayout(hbox)
        self.create_wallet_button = QPushButton(_('Create New Wallet'))
        self.create_wallet_button.setStyleSheet("font-size: 20px; padding: 8px 24px;")
        self.create_wallet_button.hide()
        self.import_seed_button = QPushButton(_('Import Seed Phrase'))
        self.import_seed_button.setStyleSheet("font-size: 20px; padding: 8px 24px;")
        self.import_seed_button.hide()
        self.setup_hw_button = QPushButton(_('Set Up Hardware Wallet'))
        self.setup_hw_button.setStyleSheet("font-size: 20px; padding: 8px 24px;")
        self.setup_hw_button.hide()
        self.button_hbox = QHBoxLayout()
        self.button_hbox.addWidget(self.create_wallet_button)
        self.button_hbox.addWidget(self.import_seed_button)
        self.button_hbox.addWidget(self.setup_hw_button)
        self.button_hbox.addStretch(1)
        self.button_hbox.addWidget(self.back_button)
        self.button_hbox.addWidget(self.next_button)
        outer_vbox.addLayout(self.button_hbox)
        self.set_icon(':icons/electron-cash.svg')
        self.show()
        self.raise_()

        # Track object lifecycle
        finalization_print_error(self)

    def _build_wallet_selection_page(self, wallet_folder, create_new_requested, import_seed_requested, setup_hw_requested):
        """Build (or rebuild) the wallet selection page layout and widgets.
        Returns the layout. All child widgets are recreated fresh each time
        so the layout can safely be discarded and rebuilt."""
        vbox = QVBoxLayout()

        # Wallet browser list
        wallet_list = QTreeWidget()
        wallet_list.setHeaderLabels([_('Name'), _('Last Modified')])
        wallet_list.setRootIsDecorated(False)
        wallet_list.setSelectionMode(QAbstractItemView.SingleSelection)
        wallet_list.setAlternatingRowColors(True)
        wallet_list.header().setStretchLastSection(True)
        vbox.addWidget(wallet_list, 1)  # stretch factor 1 so it fills available space

        # Populate wallet browser
        for fname in sorted(os.listdir(wallet_folder)):
            fpath = os.path.join(wallet_folder, fname)
            if os.path.isfile(fpath):
                mtime = os.path.getmtime(fpath)
                mtime_str = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
                item = QTreeWidgetItem([fname, mtime_str])
                wallet_list.addTopLevelItem(item)
        wallet_list.sortByColumn(1, Qt.DescendingOrder)  # most recent first
        wallet_list.header().resizeSections(QHeaderView.ResizeToContents)

        self.msg_label = QLabel('')
        self.msg_label.setStyleSheet("font-size: 14px;")
        vbox.addWidget(self.msg_label)

        self._wallet_btn_hbox = QHBoxLayout()
        self.pw_e = QLineEdit('', self)
        self.pw_e.setFixedWidth(150)
        self.pw_e.setEchoMode(2)
        self.pw_label = QLabel(_('Password') + ':')
        self._wallet_btn_hbox.addWidget(self.pw_label)
        self._wallet_btn_hbox.addWidget(self.pw_e)
        self._wallet_btn_hbox.addStretch()
        vbox.addLayout(self._wallet_btn_hbox)

        # Hidden name_e — still used internally by on_filename / storage logic
        self.name_e = QLineEdit()
        self.name_e.hide()
        vbox.addWidget(self.name_e)

        def on_filename(filename):
            path = os.path.join(wallet_folder, filename)
            try:
                self.storage = WalletStorage(path, manual_upgrades=True)
                self.next_button.setEnabled(True)
            except IOError:
                self.storage = None
                self.next_button.setEnabled(False)
            if self.storage:
                if self.storage.file_exists() and self.storage.is_encrypted():
                    msg = _("This file is encrypted.") + '\n' + _('Enter your password or choose another file.')
                    pw = True
                else:
                    msg = _("Press 'Next' to open this wallet.")
                    pw = False
            else:
                msg = _('Cannot read file')
                pw = False
            self.msg_label.setText(msg)
            if pw:
                self.pw_label.show()
                self.pw_e.show()
                self.pw_e.setFocus()
            else:
                self.pw_label.hide()
                self.pw_e.hide()

        def on_wallet_selected():
            items = wallet_list.selectedItems()
            if items:
                self.name_e.setText(items[0].text(0))

        def on_create_new():
            create_new_requested[0] = True
            self.loop.exit(2)  # kick the while loop

        self.name_e.textChanged.connect(on_filename)
        wallet_list.itemSelectionChanged.connect(on_wallet_selected)
        # Disconnect any prior connections to avoid duplicate signals on rebuild
        try:
            self.create_wallet_button.clicked.disconnect()
        except TypeError:
            pass  # no prior connections
        self.create_wallet_button.clicked.connect(on_create_new)
        self.create_wallet_button.show()

        def on_import_seed():
            import_seed_requested[0] = True
            self.loop.exit(2)

        try:
            self.import_seed_button.clicked.disconnect()
        except TypeError:
            pass  # no prior connections
        self.import_seed_button.clicked.connect(on_import_seed)
        self.import_seed_button.show()

        def on_setup_hw():
            setup_hw_requested[0] = True
            self.loop.exit(2)

        try:
            self.setup_hw_button.clicked.disconnect()
        except TypeError:
            pass  # no prior connections
        self.setup_hw_button.clicked.connect(on_setup_hw)
        self.setup_hw_button.show()

        self.pw_label.hide()
        self.pw_e.hide()
        self.back_button.setText(_('Cancel'))

        # set_layout restores buttons to the permanent bar first, then sets
        # the layout.  After that we move Cancel/Next into the content area.
        self.set_layout(vbox, title=_('Electron Cash wallet'), next_enabled=False)
        # Move Cancel/Next buttons into the content row (after set_layout
        # so they survive the old layout teardown).
        self._wallet_btn_hbox.addWidget(self.back_button)
        self._wallet_btn_hbox.addWidget(self.next_button)
        self.back_button.show()
        self.next_button.show()

        # Pre-select the wallet matching self.storage.path (e.g. when
        # opened via File → Open Recent), falling back to the most recent.
        if wallet_list.topLevelItemCount() > 0:
            target_name = os.path.basename(self.storage.path)
            matched = wallet_list.findItems(target_name, Qt.MatchExactly, 0)
            if matched:
                wallet_list.setCurrentItem(matched[0])
            else:
                wallet_list.setCurrentItem(wallet_list.topLevelItem(0))

    def run_and_get_wallet(self):

        wallet_folder = os.path.dirname(self.storage.path)
        create_new_requested = [False]
        import_seed_requested = [False]
        setup_hw_requested = [False]

        self._build_wallet_selection_page(wallet_folder, create_new_requested, import_seed_requested, setup_hw_requested)

        while True:
            password = None
            if self.loop.exec_() != 2:  # 2 = next
                return

            # --- Handle "Create New Wallet" button ---
            if create_new_requested[0]:
                create_new_requested[0] = False
                self.stack = []
                self.run('create_new_wallet', wallet_folder)
                if self.wallet:
                    return self.wallet, None
                # User went back from naming page or cancelled —
                # rebuild the wallet selection page from scratch
                # (the old layout was destroyed when set_layout replaced it)
                self._build_wallet_selection_page(wallet_folder, create_new_requested, import_seed_requested, setup_hw_requested)
                continue

            # --- Handle "Import Seed Phrase" button ---
            if import_seed_requested[0]:
                import_seed_requested[0] = False
                self.stack = []
                self.run('import_wallet_from_seed', wallet_folder)
                if self.wallet:
                    return self.wallet, None
                self._build_wallet_selection_page(wallet_folder, create_new_requested, import_seed_requested, setup_hw_requested)
                continue

            # --- Handle "Set Up Hardware Wallet" button ---
            if setup_hw_requested[0]:
                setup_hw_requested[0] = False
                self.stack = []
                self.run('setup_hardware_wallet', wallet_folder)
                if self.wallet:
                    return self.wallet, None
                self._build_wallet_selection_page(wallet_folder, create_new_requested, import_seed_requested, setup_hw_requested)
                continue

            if self.storage.file_exists() and not self.storage.is_encrypted():
                break
            if not self.storage.file_exists():
                break
            if self.storage.file_exists() and self.storage.is_encrypted():
                password = self.pw_e.text()
                try:
                    self.storage.decrypt(password)
                    break
                except InvalidPassword as e:
                    QMessageBox.information(None, _('Error'), str(e))
                    continue
                except BaseException as e:
                    traceback.print_exc(file=sys.stdout)
                    QMessageBox.information(None, _('Error'), str(e))
                    return

        self.create_wallet_button.hide()
        self.import_seed_button.hide()
        self.setup_hw_button.hide()

        path = self.storage.path
        if self.storage.requires_split():
            self.hide()
            msg = _("The wallet '{}' contains multiple accounts, which are no longer supported since Electrum 2.7.\n\n"
                    "Do you want to split your wallet into multiple files?").format(path)
            if not self.question(msg):
                return
            file_list = '\n'.join(self.storage.split_accounts())
            msg = _('Your accounts have been moved to') + ':\n' + file_list + '\n\n'+ _('Do you want to delete the old file') + ':\n' + path
            if self.question(msg):
                os.remove(path)
                self.show_warning(_('The file was removed'))
            return

        if self.storage.requires_upgrade():
            self.hide()
            msg = _("The format of your wallet '%s' must be upgraded for Electron Cash. This change will not be backward compatible"%path)
            if not self.question(msg):
                return
            self.storage.upgrade()
            self.wallet = Wallet(self.storage)
            return self.wallet, password

        action = self.storage.get_action()
        if action:
            self.hide()
            msg = _("The file '{}' contains an incompletely created wallet.\n"
                    "Do you want to complete its creation now?").format(path)
            if not self.question(msg):
                if self.question(_("Do you want to delete '{}'?").format(path)):
                    os.remove(path)
                    self.show_warning(_('The file was removed'))
                return
            self.show()
            # self.wallet is set in run
            self.run(action)
            return self.wallet, password

        self.wallet = Wallet(self.storage)
        return self.wallet, password

    def finished(self):
        """Called in hardware client wrapper, in order to close popups."""
        return

    def create_new_wallet(self, wallet_folder):
        """Stack entry for the create-new-wallet flow.
        Called via self.run('create_new_wallet', wallet_folder).
        Sets up run_next callback and shows the naming dialog."""
        def on_wallet_named(wallet_name):
            if not wallet_name:
                return
            path = os.path.join(wallet_folder, wallet_name)
            try:
                self.storage = WalletStorage(path, manual_upgrades=True)
            except IOError:
                return
            self.wallet_type = 'standard'
            self.run('create_standard_seed')
        self.create_wallet_name_dialog(run_next=on_wallet_named,
                                       wallet_folder=wallet_folder)

    @wizard_dialog
    def create_wallet_name_dialog(self, run_next, wallet_folder):
        create_vbox = QVBoxLayout()
        create_style = "font-size: 14px;"
        name_label = QLabel(_('Enter a name for your wallet:'))
        name_label.setStyleSheet(create_style)
        create_vbox.addWidget(name_label)
        name_input = QLineEdit()
        name_input.setPlaceholderText(_('Wallet name'))
        name_input.setText(get_new_wallet_name(wallet_folder))
        name_input.setStyleSheet(create_style)
        create_vbox.addWidget(name_input)
        create_vbox.addSpacing(10)
        msg1 = QLabel(_("Next, you will be shown your new wallet's automatically "
                        "generated <i>seed words</i>."))
        msg1.setWordWrap(True)
        msg1.setStyleSheet(create_style)
        create_vbox.addWidget(msg1)
        msg2 = QLabel(_("These words are a backup of your wallet, and must be "
                        "kept secret."))
        msg2.setWordWrap(True)
        msg2.setStyleSheet(create_style)
        create_vbox.addWidget(msg2)
        msg3 = QLabel(_("<b>Before continuing, make sure you are somewhere "
                        "private.</b>"))
        msg3.setWordWrap(True)
        msg3.setStyleSheet(create_style)
        create_vbox.addWidget(msg3)
        create_vbox.addStretch(1)
        self.create_wallet_button.hide()
        self.import_seed_button.hide()
        self.setup_hw_button.hide()
        self.next_button.setText(_('Continue'))
        self.back_button.setText(_('Back'))
        # Use manual loop so Back raises UserCancelled (returns to wallet
        # selection) instead of GoBack (which would close the wizard since
        # there's nothing before this on the stack).
        self.set_layout(create_vbox, title=_('Create Wallet'))
        while True:
            result = self.loop.exec_()
            if not result or result == 1:  # close or Back
                raise UserCancelled
            break  # result == 2 (Continue)
        self.title.setVisible(False)
        self.back_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.main_widget.setVisible(False)
        self.please_wait.setVisible(True)
        self.refresh_gui()
        self.next_button.setText(_('Next'))
        return name_input.text().strip()

    def import_wallet_from_seed(self, wallet_folder):
        """Stack entry for the import-seed-phrase flow.
        Called via self.run('import_wallet_from_seed', wallet_folder).
        Shows the naming dialog then enters restore_from_seed."""
        def on_wallet_named(wallet_name):
            if not wallet_name:
                return
            path = os.path.join(wallet_folder, wallet_name)
            try:
                self.storage = WalletStorage(path, manual_upgrades=True)
            except IOError:
                return
            self.wallet_type = 'standard'
            self.run('restore_from_seed')
        self.import_wallet_name_dialog(run_next=on_wallet_named,
                                       wallet_folder=wallet_folder)

    @wizard_dialog
    def import_wallet_name_dialog(self, run_next, wallet_folder):
        create_vbox = QVBoxLayout()
        create_style = "font-size: 14px;"
        name_label = QLabel(_('Enter a name for your wallet:'))
        name_label.setStyleSheet(create_style)
        create_vbox.addWidget(name_label)
        name_input = QLineEdit()
        name_input.setPlaceholderText(_('Wallet name'))
        name_input.setText(get_new_wallet_name(wallet_folder))
        name_input.setStyleSheet(create_style)
        create_vbox.addWidget(name_input)
        create_vbox.addSpacing(10)
        msg1 = QLabel(_("Next, you will be asked to enter your existing "
                         "<i>seed words</i> to restore your wallet."))
        msg1.setWordWrap(True)
        msg1.setStyleSheet(create_style)
        create_vbox.addWidget(msg1)
        create_vbox.addStretch(1)
        self.create_wallet_button.hide()
        self.import_seed_button.hide()
        self.setup_hw_button.hide()
        self.next_button.setText(_('Continue'))
        self.back_button.setText(_('Back'))
        self.set_layout(create_vbox, title=_('Import Wallet'))
        while True:
            result = self.loop.exec_()
            if not result or result == 1:  # close or Back
                raise UserCancelled
            break  # result == 2 (Continue)
        self.title.setVisible(False)
        self.back_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.main_widget.setVisible(False)
        self.please_wait.setVisible(True)
        self.refresh_gui()
        self.next_button.setText(_('Next'))
        return name_input.text().strip()

    def setup_hardware_wallet(self, wallet_folder):
        """Stack entry for the setup-hardware-wallet flow.
        Called via self.run('setup_hardware_wallet', wallet_folder).
        Shows the naming dialog then enters choose_hw_device."""
        def on_wallet_named(wallet_name):
            if not wallet_name:
                return
            path = os.path.join(wallet_folder, wallet_name)
            try:
                self.storage = WalletStorage(path, manual_upgrades=True)
            except IOError:
                return
            self.wallet_type = 'standard'
            self.run('choose_hw_device')
        self.setup_hw_wallet_name_dialog(run_next=on_wallet_named,
                                         wallet_folder=wallet_folder)

    @wizard_dialog
    def setup_hw_wallet_name_dialog(self, run_next, wallet_folder):
        create_vbox = QVBoxLayout()
        create_style = "font-size: 14px;"
        name_label = QLabel(_('Enter a name for your wallet:'))
        name_label.setStyleSheet(create_style)
        create_vbox.addWidget(name_label)
        name_input = QLineEdit()
        name_input.setPlaceholderText(_('Wallet name'))
        name_input.setText(get_new_wallet_name(wallet_folder))
        name_input.setStyleSheet(create_style)
        create_vbox.addWidget(name_input)
        create_vbox.addSpacing(10)
        msg1 = QLabel(_("Next, you will be asked to connect and select "
                         "your hardware wallet device."))
        msg1.setWordWrap(True)
        msg1.setStyleSheet(create_style)
        create_vbox.addWidget(msg1)
        create_vbox.addStretch(1)
        self.create_wallet_button.hide()
        self.import_seed_button.hide()
        self.setup_hw_button.hide()
        self.next_button.setText(_('Continue'))
        self.back_button.setText(_('Back'))
        self.set_layout(create_vbox, title=_('Set Up Hardware Wallet'))
        while True:
            result = self.loop.exec_()
            if not result or result == 1:  # close or Back
                raise UserCancelled
            break  # result == 2 (Continue)
        self.title.setVisible(False)
        self.back_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.main_widget.setVisible(False)
        self.please_wait.setVisible(True)
        self.refresh_gui()
        self.next_button.setText(_('Next'))
        return name_input.text().strip()

    def on_error(self, exc_info):
        if not isinstance(exc_info[1], UserCancelled):
            traceback.print_exception(*exc_info)
            self.show_error(str(exc_info[1]))

    def set_icon(self, filename):
        prior_filename, self.icon_filename = self.icon_filename, filename
        self.logo.setPixmap(QIcon(filename).pixmap(60))
        return prior_filename

    def _restore_button_bar(self):
        """Rescue persistent buttons back to the permanent button bar.
        Called before destroying a content layout that may contain them."""
        # Reparent to self so they survive the old layout being destroyed
        for btn in (self.back_button, self.next_button, self.create_wallet_button, self.import_seed_button, self.setup_hw_button):
            btn.setParent(self)
        # Clear old items from button_hbox
        while self.button_hbox.count():
            item = self.button_hbox.takeAt(0)
            # Delete spacer items (stretches), but not widget items
            if not item.widget():
                del item
        # Rebuild the permanent button bar
        self.button_hbox.addWidget(self.create_wallet_button)
        self.button_hbox.addWidget(self.import_seed_button)
        self.button_hbox.addWidget(self.setup_hw_button)
        self.button_hbox.addStretch(1)
        self.button_hbox.addWidget(self.back_button)
        self.button_hbox.addWidget(self.next_button)

    def set_layout(self, layout, title=None, next_enabled=True):
        self.title.setText("<b>%s</b>"%title if title else "")
        self.title.setVisible(bool(title))
        # Rescue persistent buttons before destroying old layout — they may
        # have been temporarily placed inside the content area (e.g. wallet
        # selection page) and would be destroyed along with the old layout.
        self._restore_button_bar()
        # Get rid of any prior layout by assigning it to a temporary widget
        prior_layout = self.main_widget.layout()
        if prior_layout:
            QWidget().setLayout(prior_layout)
        self.main_widget.setLayout(layout)
        self.back_button.setEnabled(True)
        self.next_button.setEnabled(bool(next_enabled))
        if next_enabled:
            self.next_button.setFocus()
        self.main_widget.setVisible(True)
        self.please_wait.setVisible(False)

    def exec_layout(self, layout, title=None, raise_on_cancel=True,
                        next_enabled=True):
        self.set_layout(layout, title, next_enabled)
        result = self.loop.exec_()
        if not result and raise_on_cancel:
            raise UserCancelled
        if result == 1:
            raise GoBack
        self.title.setVisible(False)
        self.back_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.main_widget.setVisible(False)
        self.please_wait.setVisible(True)
        self.refresh_gui()
        return result

    def refresh_gui(self):
        # For some reason, to refresh the GUI this needs to be called twice
        self.app.processEvents()
        self.app.processEvents()

    def remove_from_recently_open(self, filename):
        self.config.remove_from_recently_open(filename)

    def text_input(self, title, message, is_valid, allow_multi=False):
        slayout = KeysLayout(parent=self, title=message, is_valid=is_valid,
                             allow_multi=allow_multi)
        self.exec_layout(slayout, title, next_enabled=False)
        return slayout.get_text()

    def seed_input(self, title, message, is_seed, options):
        slayout = SeedLayout(title=message, is_seed=is_seed, options=options, parent=self, editable=True)
        self.exec_layout(slayout, title, next_enabled=False)
        return slayout.get_seed(), slayout.is_bip39, slayout.is_ext

    def bip38_prompt_for_pw(self, bip38_keys):
        ''' Reimplemented from basewizard superclass. Expected to return the pw
        dict or None. '''
        d = Bip38Importer(bip38_keys, parent=self.top_level_window())
        res = d.exec_()
        d.setParent(None)  # python GC quicker if this happens
        return d.decoded_keys  # dict will be empty if user cancelled

    @wizard_dialog
    def add_xpub_dialog(self, title, message, is_valid, run_next, allow_multi=False):
        return self.text_input(title, message, is_valid, allow_multi)

    @wizard_dialog
    def add_cosigner_dialog(self, run_next, index, is_valid):
        title = _("Add Cosigner") + " %d"%index
        message = ' '.join([
            _('Please enter the master public key (xpub) of your cosigner.'),
            _('Enter their master private key (xprv) if you want to be able to sign for them.')
        ])
        return self.text_input(title, message, is_valid)

    @wizard_dialog
    def restore_seed_dialog(self, run_next, test):
        from electroncash import mnemonic as mn
        mnemo = mn.Mnemonic('en')

        options = []
        if self.opt_ext:
            options.append('ext')
        if self.opt_bip39:
            options.append('bip39')

        vbox = QVBoxLayout()
        words_count = 12

        # Message label at 14px
        msg_label = QLabel(_('Please enter your seed phrase in order to restore your wallet.'))
        msg_label.setStyleSheet("font-size: 14px;")
        msg_label.setWordWrap(True)
        vbox.addWidget(msg_label)

        # Subtitle
        subtitle = QLabel(_('Hit spacebar to move to next word'))
        subtitle.setStyleSheet("font-size: 13px; color: gray;")
        subtitle.setAlignment(Qt.AlignCenter)
        vbox.addWidget(subtitle)

        # Grid
        grid = QGridLayout()
        grid.setSpacing(8)

        # Card/input styles (same as confirm_seed_dialog)
        if ColorScheme.dark_scheme:
            card_style = "QFrame { border: 1px solid #555; border-radius: 8px; background: #3a3a3a; padding: 4px; }"
            num_color = "#6ea8d9"
            input_style = "QLineEdit { font-size: 20px; border: 1px solid #555; border-radius: 4px; background: transparent; color: white; padding: 2px; }"
            error_input_style = "QLineEdit { font-size: 20px; border: 1px solid #555; border-radius: 4px; background: #5a2a2a; color: white; padding: 2px; }"
        else:
            card_style = "QFrame { border: 1px solid #ccc; border-radius: 8px; background: white; padding: 4px; }"
            num_color = "#5b9bd5"
            input_style = "QLineEdit { font-size: 20px; border: 1px solid #ccc; border-radius: 4px; background: transparent; padding: 2px; }"
            error_input_style = "QLineEdit { font-size: 20px; border: 1px solid #ccc; border-radius: 4px; background: #ffcccc; padding: 2px; }"

        # SeedInputFilter — spacebar advances to next input, focus-out
        # validates word against BIP39 dictionary.
        class SeedInputFilter(QObject):
            def __init__(self, inputs, wordlist_indices,
                         normal_style, error_style):
                super().__init__()
                self.inputs = inputs
                self.wordlist_indices = wordlist_indices
                self.normal_style = normal_style
                self.error_style = error_style

            def eventFilter(self, obj, event):
                if (event.type() == QEvent.KeyPress
                        and event.key() == Qt.Key_Space):
                    try:
                        idx = self.inputs.index(obj)
                    except ValueError:
                        return False
                    if idx < len(self.inputs) - 1:
                        self.inputs[idx + 1].setFocus()
                    return True  # consume the space character
                if event.type() == QEvent.FocusOut:
                    word = obj.text().strip().lower()
                    if word and word not in self.wordlist_indices:
                        obj.setStyleSheet(self.error_style)
                    else:
                        obj.setStyleSheet(self.normal_style)
                return False

        inputs = []
        seed_filter = SeedInputFilter(inputs, mnemo.wordlist_indices,
                                      input_style, error_input_style)

        # Build 12 cards (2 rows × 6 cols)
        for i in range(words_count):
            row = i // 6
            col = i % 6

            frame = QFrame()
            frame.setStyleSheet(card_style)
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(6, 4, 6, 8)
            frame_layout.setSpacing(0)

            num_label = QLabel(str(i + 1))
            num_label.setStyleSheet(f"color: {num_color}; font-size: 20px; border: none; background: transparent;")
            num_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)

            word_input = QLineEdit()
            word_input.setStyleSheet(input_style)
            word_input.setAlignment(Qt.AlignCenter)
            word_input.installEventFilter(seed_filter)

            frame_layout.addWidget(num_label)
            frame_layout.addWidget(word_input)

            grid.addWidget(frame, row, col)
            inputs.append(word_input)

        vbox.addLayout(grid)

        # Mutable state for options
        is_bip39 = [False]
        is_ext = [False]
        saved_test = test  # original validation function

        # Seed type label
        seed_type_label = QLabel('')
        seed_type_label.setStyleSheet("font-size: 12px;")

        # Validation handler (called on every text change)
        def validate_seed():
            entered = ' '.join(inp.text().strip().lower()
                               for inp in inputs)
            current_test = ((lambda x: bool(x)) if is_bip39[0]
                            else saved_test)
            valid = bool(current_test(entered))
            self.next_button.setEnabled(valid)
            # Update seed type label
            if not is_bip39[0]:
                t = mn.format_seed_type_name_for_ui(
                    mn.seed_type_name(entered))
                seed_type_label.setText(
                    (_('Seed Type') + ': ' + t) if t else '')
            else:
                is_checksum, is_wordlist = mnemo.is_checksum_valid(
                    entered)
                status = (('checksum: '
                           + ('ok' if is_checksum else 'failed'))
                          if is_wordlist else 'unknown wordlist')
                seed_type_label.setText('BIP39 (%s)' % status)

        # Per-input text change handlers (paste distribution +
        # validation)
        def make_handler(idx):
            def handler(text):
                words = text.strip().split()
                if len(words) > 1:
                    for j, w in enumerate(words):
                        target = idx + j
                        if target < len(inputs):
                            inputs[target].blockSignals(True)
                            inputs[target].setText(w.lower())
                            inputs[target].blockSignals(False)
                    next_focus = min(idx + len(words),
                                     len(inputs) - 1)
                    inputs[next_focus].setFocus()
                validate_seed()
            return handler

        for i, inp in enumerate(inputs):
            inp.textChanged.connect(make_handler(i))

        # Options button + seed type label row
        hbox = QHBoxLayout()
        hbox.addStretch(1)
        hbox.addWidget(seed_type_label)
        if options:
            def open_options():
                dialog = QDialog(self)
                dvbox = QVBoxLayout(dialog)
                cb_ext = cb_bip39 = None
                if 'ext' in options:
                    cb_ext = QCheckBox(
                        _('Extend this seed with custom words')
                        + " " + _("(aka 'passphrase')"))
                    cb_ext.setChecked(is_ext[0])
                    dvbox.addWidget(cb_ext)
                if 'bip39' in options:
                    cb_bip39 = QCheckBox(
                        _('Force BIP39 interpretation of this seed'))
                    cb_bip39.setChecked(is_bip39[0])
                    dvbox.addWidget(cb_bip39)
                dvbox.addLayout(Buttons(OkButton(dialog)))
                if not dialog.exec_():
                    return
                if cb_ext:
                    is_ext[0] = cb_ext.isChecked()
                if cb_bip39:
                    is_bip39[0] = cb_bip39.isChecked()
                validate_seed()  # re-validate with new options

            opt_button = QPushButton(_('Options'))
            opt_button.clicked.connect(open_options)
            hbox.addWidget(opt_button)
        vbox.addLayout(hbox)
        vbox.addStretch(1)

        self.exec_layout(vbox, title=_('Enter Seed'), next_enabled=False)

        entered = ' '.join(inp.text().strip().lower() for inp in inputs)
        return entered, is_bip39[0], is_ext[0]

    @wizard_dialog
    def confirm_seed_dialog(self, run_next, test):
        self.app.clipboard().clear()
        from electroncash import mnemonic as mn
        mnemo = mn.Mnemonic('en')

        vbox = QVBoxLayout()
        words_count = 12

        grid = QGridLayout()
        grid.setSpacing(8)

        if ColorScheme.dark_scheme:
            card_style = "QFrame { border: 1px solid #555; border-radius: 8px; background: #3a3a3a; padding: 4px; }"
            num_color = "#6ea8d9"
            input_style = "QLineEdit { font-size: 20px; border: 1px solid #555; border-radius: 4px; background: transparent; color: white; padding: 2px; }"
            error_input_style = "QLineEdit { font-size: 20px; border: 1px solid #555; border-radius: 4px; background: #5a2a2a; color: white; padding: 2px; }"
        else:
            card_style = "QFrame { border: 1px solid #ccc; border-radius: 8px; background: white; padding: 4px; }"
            num_color = "#5b9bd5"
            input_style = "QLineEdit { font-size: 20px; border: 1px solid #ccc; border-radius: 4px; background: transparent; padding: 2px; }"
            error_input_style = "QLineEdit { font-size: 20px; border: 1px solid #ccc; border-radius: 4px; background: #ffcccc; padding: 2px; }"

        confirm_greyed_style = "font-size: 24px; padding: 8px 24px; color: gray;"
        confirm_active_style = "font-size: 24px; padding: 8px 24px;"
        seed_confirmed = [False]

        # Event filter: spacebar advances to next input, focus-out
        # validates word against BIP39 dictionary.
        class SeedInputFilter(QObject):
            def __init__(self, inputs, wordlist_indices,
                         normal_style, error_style):
                super().__init__()
                self.inputs = inputs
                self.wordlist_indices = wordlist_indices
                self.normal_style = normal_style
                self.error_style = error_style

            def eventFilter(self, obj, event):
                if (event.type() == QEvent.KeyPress
                        and event.key() == Qt.Key_Space):
                    try:
                        idx = self.inputs.index(obj)
                    except ValueError:
                        return False
                    if idx < len(self.inputs) - 1:
                        self.inputs[idx + 1].setFocus()
                    return True  # consume the space character
                if event.type() == QEvent.FocusOut:
                    word = obj.text().strip().lower()
                    if word and word not in self.wordlist_indices:
                        obj.setStyleSheet(self.error_style)
                    else:
                        obj.setStyleSheet(self.normal_style)
                return False

        inputs = []
        seed_filter = SeedInputFilter(inputs, mnemo.wordlist_indices,
                                      input_style, error_input_style)
        for i in range(words_count):
            row = i // 6
            col = i % 6

            frame = QFrame()
            frame.setStyleSheet(card_style)
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(6, 4, 6, 8)
            frame_layout.setSpacing(0)

            num_label = QLabel(str(i + 1))
            num_label.setStyleSheet(f"color: {num_color}; font-size: 20px; border: none; background: transparent;")
            num_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)

            word_input = QLineEdit()
            word_input.setStyleSheet(input_style)
            word_input.setAlignment(Qt.AlignCenter)
            word_input.installEventFilter(seed_filter)

            frame_layout.addWidget(num_label)
            frame_layout.addWidget(word_input)

            # Match card sizes from the seed display page
            if hasattr(self, '_seed_card_sizes') and i < len(self._seed_card_sizes):
                frame.setMinimumSize(self._seed_card_sizes[i])

            grid.addWidget(frame, row, col)
            inputs.append(word_input)

        def make_handler(idx):
            def handler(text):
                # Handle paste: if text contains spaces, distribute words
                words = text.strip().split()
                if len(words) > 1:
                    for j, w in enumerate(words):
                        target = idx + j
                        if target < len(inputs):
                            inputs[target].blockSignals(True)
                            inputs[target].setText(w.lower())
                            inputs[target].blockSignals(False)
                    # Focus the field after the last pasted word
                    next_focus = min(idx + len(words), len(inputs) - 1)
                    inputs[next_focus].setFocus()
                # Validate full seed — update visual state (button stays enabled)
                entered = ' '.join(inp.text().strip().lower() for inp in inputs)
                seed_confirmed[0] = bool(test(entered))
                if seed_confirmed[0]:
                    self.next_button.setStyleSheet(confirm_active_style)
                else:
                    self.next_button.setStyleSheet(confirm_greyed_style)
            return handler

        for i, inp in enumerate(inputs):
            inp.textChanged.connect(make_handler(i))

        subtitle = QLabel(_('Hit spacebar to move to next word'))
        subtitle.setStyleSheet("font-size: 13px; color: gray;")
        subtitle.setAlignment(Qt.AlignCenter)
        vbox.addWidget(subtitle)
        vbox.addLayout(grid)
        vbox.addStretch(1)

        self.next_button.setText(_('Confirm'))
        self.set_layout(vbox, title=_("Enter your seed words as you've recorded them to confirm."), next_enabled=True)
        self.next_button.setStyleSheet(confirm_greyed_style)
        while True:
            result = self.loop.exec_()
            if not result:
                raise UserCancelled
            if result == 1:  # Back clicked
                warn = QMessageBox(self)
                warn.setWindowTitle(_('Warning'))
                warn.setTextFormat(Qt.RichText)
                warn.setText(_('Going back will start the new wallet process over.<br>'
                               'That means you will be given <b>new seed words</b>.'))
                warn.setInformativeText(_('Do you want to go back?'))
                go_back_btn = warn.addButton(_('Go Back'), QMessageBox.AcceptRole)
                stay_btn = warn.addButton(_('Stay on this page'), QMessageBox.RejectRole)
                warn.setDefaultButton(stay_btn)
                warn.exec_()
                if warn.clickedButton() == go_back_btn:
                    # Pop 'confirm_seed' so go_back() skips
                    # 'create_standard_seed' and lands on
                    # 'create_new_wallet' (the naming dialog).
                    self.stack.pop()
                    raise GoBack
                continue
            # result == 2 (Confirm clicked)
            if not seed_confirmed[0]:
                warn = QMessageBox(self)
                warn.setWindowTitle(_('Warning'))
                warn.setTextFormat(Qt.RichText)
                warn.setText(_("What you've entered on this page doesn't "
                               "match your seed words.<br><br>"
                               "If you don't back up your seed words and "
                               "then lose access to your device, you will "
                               "be unable to recover any funds in the "
                               "wallet.<br><br>"
                               "Do you really want to skip confirming your "
                               "seed words?"))
                stay_btn = warn.addButton(_('Stay on page and confirm'),
                                          QMessageBox.AcceptRole)
                skip_btn = warn.addButton(_('Skip confirming and continue'),
                                          QMessageBox.RejectRole)
                warn.setDefaultButton(stay_btn)
                warn.exec_()
                if warn.clickedButton() == stay_btn:
                    continue
            break  # Confirmed or user chose to skip
        self.next_button.setStyleSheet("font-size: 24px; padding: 8px 24px;")
        self.title.setVisible(False)
        self.back_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.main_widget.setVisible(False)
        self.please_wait.setVisible(True)
        self.refresh_gui()
        self.next_button.setText(_('Next'))

        entered = ' '.join(inp.text().strip().lower() for inp in inputs)
        return entered

    @wizard_dialog
    def show_seed_dialog(self, run_next, seed_text, editable=True):
        vbox = QVBoxLayout()
        words = seed_text.split()

        # Word grid: 2 rows × 6 columns (adapts for longer seeds)
        grid = QGridLayout()
        grid.setSpacing(8)

        if ColorScheme.dark_scheme:
            card_style = "QFrame { border: 1px solid #555; border-radius: 8px; background: #3a3a3a; padding: 4px; }"
            num_color = "#6ea8d9"
        else:
            card_style = "QFrame { border: 1px solid #ccc; border-radius: 8px; background: white; padding: 4px; }"
            num_color = "#5b9bd5"

        frames = []
        for i, word in enumerate(words):
            row = i // 6
            col = i % 6

            frame = QFrame()
            frame.setStyleSheet(card_style)
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(6, 4, 6, 8)
            frame_layout.setSpacing(0)

            num_label = QLabel(str(i + 1))
            num_label.setStyleSheet(f"color: {num_color}; font-size: 20px; border: none; background: transparent;")
            num_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)

            word_label = QLabel(word)
            word_label.setStyleSheet("font-size: 20px; border: none; background: transparent;")
            word_label.setAlignment(Qt.AlignCenter)

            frame_layout.addWidget(num_label)
            frame_layout.addWidget(word_label)

            grid.addWidget(frame, row, col)
            frames.append(frame)

        vbox.addLayout(grid)
        vbox.addSpacing(16)

        # Warning messages
        msg_style = "font-size: 14px;"
        msg1 = QLabel(_("Write these words down on a <b>physical piece of paper</b> and store that paper somewhere safe."))
        msg1.setWordWrap(True)
        msg1.setStyleSheet(msg_style)
        vbox.addWidget(msg1)
        msg2 = QLabel(_("Storing these words electronically (for example, in a text file or picture) is much less secure."))
        msg2.setWordWrap(True)
        msg2.setStyleSheet(msg_style)
        vbox.addWidget(msg2)
        msg3 = QLabel(_("<b>Anyone who has these words can spend the funds in your wallet.</b>"))
        msg3.setWordWrap(True)
        msg3.setStyleSheet(msg_style)
        vbox.addWidget(msg3)

        vbox.addStretch(1)

        self.next_button.setText(_('Confirm'))
        self.set_layout(vbox, title=_("These are your new wallet's seed words:"))
        while True:
            result = self.loop.exec_()
            if not result:
                raise UserCancelled
            if result == 1:  # Back clicked
                warn = QMessageBox(self)
                warn.setWindowTitle(_('Warning'))
                warn.setTextFormat(Qt.RichText)
                warn.setText(_('Going back will start the new wallet process over.<br>'
                               'That means you will be given <b>new seed words</b>.'))
                warn.setInformativeText(_('Do you want to go back?'))
                go_back_btn = warn.addButton(_('Go Back'), QMessageBox.AcceptRole)
                stay_btn = warn.addButton(_('Stay on this page'), QMessageBox.RejectRole)
                warn.setDefaultButton(stay_btn)
                warn.exec_()
                if warn.clickedButton() == go_back_btn:
                    raise GoBack
                continue
            break  # result == 2 (Confirm)
        self.title.setVisible(False)
        self.back_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.main_widget.setVisible(False)
        self.please_wait.setVisible(True)
        self.refresh_gui()
        self.next_button.setText(_('Next'))

        # Record rendered card sizes so confirm_seed_dialog can match them
        self._seed_card_sizes = [f.size() for f in frames]

        return False  # no seed extension

    def pw_layout(self, msg, kind):
        playout = PasswordLayout(None, msg, kind, self.next_button)
        playout.encrypt_cb.setChecked(True)
        pw_widget = QWidget()
        pw_widget.setStyleSheet("font-size: 14px;")
        pw_widget.setLayout(playout.layout())
        vbox = QVBoxLayout()
        vbox.addWidget(pw_widget)
        vbox.addStretch(1)
        self.exec_layout(vbox)
        return playout.new_password(), playout.encrypt_cb.isChecked()

    @wizard_dialog
    def request_password(self, run_next):
        """Request the user enter a new password and confirm it.  Return
        the password or None for no password."""
        playout = PasswordLayout(None, MSG_ENTER_PASSWORD, PW_NEW,
                                 self.next_button)
        playout.encrypt_cb.setChecked(True)
        pw_widget = QWidget()
        pw_widget.setStyleSheet("font-size: 14px;")
        pw_widget.setLayout(playout.layout())
        vbox = QVBoxLayout()
        vbox.addWidget(pw_widget)
        vbox.addStretch(1)
        self.set_layout(vbox)
        while True:
            result = self.loop.exec_()
            if not result:
                raise UserCancelled
            if result == 1:  # Back clicked
                warn = QMessageBox(self)
                warn.setWindowTitle(_('Warning'))
                warn.setTextFormat(Qt.RichText)
                warn.setText(
                    _('Going back will start the new wallet process '
                      'over.<br>That means you will be given '
                      '<b>new seed words</b>.'))
                warn.setInformativeText(_('Do you want to go back?'))
                go_back_btn = warn.addButton(
                    _('Go Back'), QMessageBox.AcceptRole)
                stay_btn = warn.addButton(
                    _('Stay on this page'), QMessageBox.RejectRole)
                warn.setDefaultButton(stay_btn)
                warn.exec_()
                if warn.clickedButton() == go_back_btn:
                    # Pop 'create_wallet', 'create_keystore', and
                    # 'confirm_seed' so go_back() skips
                    # 'create_standard_seed' and lands on
                    # 'create_new_wallet' (naming dialog).
                    self.stack.pop()  # pop 'create_wallet'
                    self.stack.pop()  # pop 'create_keystore'
                    self.stack.pop()  # pop 'confirm_seed'
                    raise GoBack
                continue
            break  # result == 2 (Next)
        self.title.setVisible(False)
        self.back_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.main_widget.setVisible(False)
        self.please_wait.setVisible(True)
        self.refresh_gui()
        return playout.new_password(), playout.encrypt_cb.isChecked()

    @staticmethod
    def _add_extra_button_to_layout(extra_button, layout):
        if (not isinstance(extra_button, (list, tuple))
                or not len(extra_button) == 2):
            return
        but_title, but_action = extra_button
        hbox = QHBoxLayout()
        hbox.setContentsMargins(12,24,12,12)
        but = QPushButton(but_title)
        hbox.addStretch(1)
        hbox.addWidget(but)
        layout.addLayout(hbox)
        but.clicked.connect(but_action)

    @wizard_dialog
    def confirm_dialog(self, title, message, run_next, extra_button=None):
        self.confirm(message, title, extra_button=extra_button)

    def confirm(self, message, title, extra_button=None):
        label = WWLabel(message)
        label.setStyleSheet("font-size: 14px;")

        textInteractionFlags = (Qt.LinksAccessibleByMouse
                                | Qt.TextSelectableByMouse
                                | Qt.TextSelectableByKeyboard
                                | Qt.LinksAccessibleByKeyboard)
        label.setTextInteractionFlags(textInteractionFlags)
        label.setOpenExternalLinks(True)

        vbox = QVBoxLayout()
        vbox.addWidget(label)
        if extra_button:
            self._add_extra_button_to_layout(extra_button, vbox)
        vbox.addStretch(1)
        self.exec_layout(vbox, title)

    @wizard_dialog
    def action_dialog(self, action, run_next):
        self.run(action)

    def terminate(self):
        self.accept_signal.emit()

    def waiting_dialog(self, task, msg):
        self.please_wait.setText(MSG_GENERATING_WAIT)
        self.refresh_gui()
        t = threading.Thread(target = task)
        t.start()
        t.join()

    @wizard_dialog
    def choice_dialog(self, title, message, choices, run_next, extra_button=None):
        c_values = [x[0] for x in choices]
        c_titles = [x[1] for x in choices]
        clayout = ChoicesLayout(message, c_titles)
        choice_widget = QWidget()
        choice_widget.setStyleSheet("font-size: 14px;")
        choice_widget.setLayout(clayout.layout())
        vbox = QVBoxLayout()
        vbox.addWidget(choice_widget)
        if extra_button:
            self._add_extra_button_to_layout(extra_button, vbox)
        vbox.addStretch(1)
        self.exec_layout(vbox, title)
        action = c_values[clayout.selected_index()]
        return action

    def query_choice(self, msg, choices):
        """called by hardware wallets"""
        clayout = ChoicesLayout(msg, choices)
        vbox = QVBoxLayout()
        vbox.addLayout(clayout.layout())
        self.exec_layout(vbox, '')
        return clayout.selected_index()

    @wizard_dialog
    def input_date_dialog(self, run_next, title, message, default_time, minimum_time=0, maximum_time=None):
        vbox = QVBoxLayout()
        vbox.addWidget(WWLabel(message))
        de = QDateEdit()
        de.setDateTime(QDateTime.fromTime_t(default_time))
        de.setMinimumDateTime(QDateTime.fromSecsSinceEpoch(int(minimum_time)))
        de.setCalendarPopup(True)  # Enable calendar popup
        if maximum_time is not None and maximum_time >= minimum_time:
            de.setMaximumDateTime(QDateTime.fromSecsSinceEpoch(int(maximum_time)))
        de.setDisplayFormat("MMMM dd yyyy")
        def test():
            d = de.dateTime().toSecsSinceEpoch()
            mn = de.minimumDateTime().toSecsSinceEpoch()
            mx = de.maximumDateTime().toSecsSinceEpoch()
            is_ok = mn <= d <= mx
            self.next_button.setEnabled(is_ok)
            return is_ok
        de.dateChanged.connect(test)
        vbox.addWidget(de)
        self.exec_layout(vbox, title, next_enabled=test())
        return de.dateTime().toSecsSinceEpoch()

    @wizard_dialog
    def line_dialog(self, run_next, title, message, default, test, warning=''):
        vbox = QVBoxLayout()
        line_style = "font-size: 14px;"
        msg_lbl = WWLabel(message)
        msg_lbl.setStyleSheet(line_style)
        vbox.addWidget(msg_lbl)
        line = QLineEdit()
        line.setStyleSheet(line_style)
        line.setText(default)
        def f(text):
            self.next_button.setEnabled(bool(test(text)))
        line.textEdited.connect(f)
        vbox.addWidget(line)
        warn_lbl = WWLabel(warning)
        warn_lbl.setStyleSheet(line_style)
        vbox.addWidget(warn_lbl)
        vbox.addStretch(1)
        self.exec_layout(vbox, title, next_enabled=test(default))
        return ' '.join(line.text().split())

    @wizard_dialog
    def derivation_path_dialog(self, run_next, title, message, default, test, warning='', seed='', scannable=False):
        def on_derivation_scan(derivation_line, seed):
            derivation_scan_dialog = DerivationDialog(self, seed, DerivationPathScanner.DERIVATION_PATHS)
            destroyed_print_error(derivation_scan_dialog)
            selected_path = derivation_scan_dialog.get_selected_path()
            if selected_path:
                derivation_line.setText(selected_path)
            derivation_scan_dialog.deleteLater()

        vbox = QVBoxLayout()
        style_14 = "font-size: 14px;"
        msg_lbl = WWLabel(message)
        msg_lbl.setStyleSheet(style_14)
        vbox.addWidget(msg_lbl)
        line = QLineEdit()
        line.setStyleSheet(style_14)
        line.setText(default)
        def f(text):
            self.next_button.setEnabled(bool(test(text)))
        line.textEdited.connect(f)
        vbox.addWidget(line)
        warn_lbl = WWLabel(warning)
        warn_lbl.setStyleSheet(style_14)
        vbox.addWidget(warn_lbl)

        if scannable:
            hbox = QHBoxLayout()
            hbox.setContentsMargins(12,24,12,12)
            but = QPushButton(_("Scan Derivation Paths..."))
            but.setStyleSheet(style_14)
            hbox.addStretch(1)
            hbox.addWidget(but)
            vbox.addLayout(hbox)
            but.clicked.connect(lambda: on_derivation_scan(line, seed))

        vbox.addStretch(1)
        self.exec_layout(vbox, title, next_enabled=test(default))
        return ' '.join(line.text().split())

    @wizard_dialog
    def show_xpub_dialog(self, xpub, run_next):
        msg = ' '.join([
            _("Here is your master public key."),
            _("Please share it with your cosigners.")
        ])
        vbox = QVBoxLayout()
        layout = SeedLayout(xpub, title=msg, icon=False)
        vbox.addLayout(layout.layout())
        self.exec_layout(vbox, _('Master Public Key'))
        return None

    def init_network(self, network):
        message = _("Electron Cash communicates with remote servers to get "
                    "information about your transactions and addresses. The "
                    "servers all fulfil the same purpose only differing in "
                    "hardware. In most cases you simply want to let Electron Cash "
                    "pick one at random.  However if you prefer feel free to "
                    "select a server manually.")
        choices = [_("Auto connect"), _("Select server manually")]
        title = _("How do you want to connect to a server? ")
        clayout = ChoicesLayout(message, choices)
        self.back_button.setText(_('Cancel'))
        self.exec_layout(clayout.layout(), title)
        r = clayout.selected_index()
        network.auto_connect = (r == 0)
        self.config.set_key('auto_connect', network.auto_connect, True)
        if r == 1:
            nlayout = NetworkChoiceLayout(self, network, self.config, wizard=True)
            if self.exec_layout(nlayout.layout()):
                nlayout.accept()

    @wizard_dialog
    def multisig_dialog(self, run_next):
        cw = CosignWidget(2, 2)
        m_edit = QSlider(Qt.Horizontal, self)
        n_edit = QSlider(Qt.Horizontal, self)
        n_edit.setMinimum(1)
        n_edit.setMaximum(15)
        m_edit.setMinimum(1)
        m_edit.setMaximum(2)
        n_edit.setValue(2)
        m_edit.setValue(2)
        n_label = QLabel()
        m_label = QLabel()
        grid = QGridLayout()
        grid.addWidget(n_label, 0, 0)
        grid.addWidget(n_edit, 0, 1)
        grid.addWidget(m_label, 1, 0)
        grid.addWidget(m_edit, 1, 1)
        def on_m(m):
            m_label.setText(_('Require %d signatures')%m)
            cw.set_m(m)
        def on_n(n):
            n_label.setText(_('From %d cosigners')%n)
            cw.set_n(n)
            m_edit.setMaximum(n)
        n_edit.valueChanged.connect(on_n)
        m_edit.valueChanged.connect(on_m)
        on_n(2)
        on_m(2)
        vbox = QVBoxLayout()
        vbox.addWidget(cw)
        vbox.addWidget(WWLabel(_("Choose the number of signatures needed to unlock funds in your wallet:")))
        vbox.addLayout(grid)
        self.exec_layout(vbox, _("Multi-Signature Wallet"))
        m = int(m_edit.value())
        n = int(n_edit.value())
        return (m, n)

    linux_hw_wallet_support_dialog = None

    def on_hw_wallet_support(self):
        ''' Overrides base wizard's noop impl. '''
        if sys.platform.startswith("linux"):
            if self.linux_hw_wallet_support_dialog:
                self.linux_hw_wallet_support_dialog.raise_()
                return
            # NB: this should only be imported from Linux
            from . import udev_installer
            self.linux_hw_wallet_support_dialog = udev_installer.InstallHardwareWalletSupportDialog(self.top_level_window(), self.plugins)
            self.linux_hw_wallet_support_dialog.exec_()
            self.linux_hw_wallet_support_dialog.setParent(None)
            self.linux_hw_wallet_support_dialog = None
        else:
            self.show_error("Linux only facility. FIXME!")


class DerivationPathScanner(QThread):

    DERIVATION_PATHS = [
        "m/44'/145'/0'",
        "m/44'/0'/0'",
        "m/44'/245'/0'",
        "m/144'/44'/0'",
        "m/144'/0'/0'",
        "m/44'/0'/0'/0",
        "m/0",
        "m/0'",
        "m/0'/0",
        "m/0'/0'",
        "m/0'/0'/0'",
        "m/44'/145'/0'/0",
        "m/44'/245'/0",
        "m/44'/245'/0'/0",
        "m/49'/0'/0'",
        "m/84'/0'/0'",
    ]

    def __init__(self, parent, seed, seed_type, config, update_table_cb):
        QThread.__init__(self, parent)
        self.update_table_cb = update_table_cb
        self.seed = seed
        self.seed_type = seed_type
        self.config = config
        self.aborting = False

    def notify_offline(self):
        for i, p in enumerate(self.DERIVATION_PATHS):
            self.update_table_cb(i, _('Offline'))

    def notify_timedout(self, i):
        self.update_table_cb(i, _('Timed out'))

    def run(self):
        network = Network.get_instance()
        if not network:
            self.notify_offline()
            return

        for i, p in enumerate(self.DERIVATION_PATHS):
            if self.aborting:
                return
            k = keystore.from_seed(self.seed, '', derivation=p, seed_type=self.seed_type)
            p_safe = p.replace('/', '_').replace("'", 'h')
            storage_path = os.path.join(
                tempfile.gettempdir(),
                p_safe + '_' + random.getrandbits(32).to_bytes(4, 'big').hex()[:8] + "_not_saved_"
            )
            tmp_storage = WalletStorage(storage_path, in_memory_only=True)
            tmp_storage.put('seed_type', self.seed_type)
            tmp_storage.put('keystore', k.dump())
            wallet = Standard_Wallet(tmp_storage)
            try:
                wallet.start_threads(network)
                wallet.synchronize()
                wallet.print_error("Scanning", p)
                synched = False
                for ctr in range(25):
                    try:
                        wallet.wait_until_synchronized(timeout=1.0)
                        synched = True
                    except TimeoutException:
                        wallet.print_error(f'timeout try {ctr+1}/25')
                    if self.aborting:
                        return
                if not synched:
                    wallet.print_error("Timeout on", p)
                    self.notify_timedout(i)
                    continue
                while network.is_connecting():
                    time.sleep(0.1)
                    if self.aborting:
                        return
                num_tx = len(wallet.get_history())
                self.update_table_cb(i, str(num_tx))
            finally:
                wallet.clear_history()
                wallet.stop_threads()


class DerivationDialog(QDialog):
    scan_result_signal = pyqtSignal(object, object)

    def __init__(self, parent, seed, paths):
        QDialog.__init__(self, parent)

        self.seed = seed
        self.seed_type = parent.seed_type
        self.config = parent.config
        self.max_seen = 0

        self.setWindowTitle(_('Select Derivation Path'))
        vbox = QVBoxLayout()
        self.setLayout(vbox)
        vbox.setContentsMargins(24, 24, 24, 24)

        self.label = QLabel(self)
        vbox.addWidget(self.label)

        self.table = QTableWidget(self)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.setSortingEnabled(False)
        self.table.setColumnCount(2)
        self.table.setRowCount(len(paths))
        self.table.setHorizontalHeaderItem(0, QTableWidgetItem(_('Path')))
        self.table.setHorizontalHeaderItem(1, QTableWidgetItem(_('Transactions')))
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setMinimumHeight(350)

        for row, d_path in enumerate(paths):
            path_item = QTableWidgetItem(d_path)
            path_item.setFlags(Qt.ItemIsSelectable|Qt.ItemIsEnabled)
            self.table.setItem(row, 0, path_item)
            transaction_count_item = QTableWidgetItem(_('Scanning...'))
            transaction_count_item.setFlags(Qt.ItemIsSelectable|Qt.ItemIsEnabled)
            self.table.setItem(row, 1, transaction_count_item)

        self.table.cellDoubleClicked.connect(self.accept)
        self.table.selectRow(0)
        vbox.addWidget(self.table)
        ok_but = OkButton(self)
        buts = Buttons(CancelButton(self), ok_but)
        vbox.addLayout(buts)
        vbox.addStretch(1)
        ok_but.setEnabled(True)
        self.scan_result_signal.connect(self.update_table)
        self.t = None

    def set_scan_progress(self, n):
        self.label.setText(_('Scanned {}/{}').format(n, len(DerivationPathScanner.DERIVATION_PATHS)))

    def kill_t(self):
        if self.t and self.t.isRunning():
            self.t.aborting = True
            self.t.wait(5000)

    def showEvent(self, e):
        super().showEvent(e)
        if e.isAccepted():
            self.kill_t()
            self.t = DerivationPathScanner(self, self.seed, self.seed_type, self.config, self.update_table_cb)
            self.max_seen = 0
            self.set_scan_progress(0)
            self.t.start()

    def closeEvent(self, e):
        super().closeEvent(e)
        if e.isAccepted():
            self.kill_t()

    def update_table_cb(self, row, scan_result):
        self.scan_result_signal.emit(row, scan_result)

    def update_table(self, row, scan_result):
        self.set_scan_progress(row+1)
        try:
            num = int(scan_result)
            if num > self.max_seen:
                self.table.selectRow(row)
                self.max_seen = num
        except (ValueError, TypeError):
            pass
        self.table.item(row, 1).setText(scan_result)

    def get_selected_path(self):
        path_to_return = None
        if self.exec_():
            pathstr = self.table.selectionModel().selectedRows()
            row = pathstr[0].row()
            path_to_return = self.table.item(row, 0).text()
        self.kill_t()
        return path_to_return
