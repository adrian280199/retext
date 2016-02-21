# vim: ts=8:sts=8:sw=8:noexpandtab
#
# This file is part of ReText
# Copyright: 2015 Dmitry Shachnev
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from markups import get_markup_for_file_name
from markups.common import MODULE_HOME_PAGE

from ReText import app_version, enchant, enchant_available, globalSettings
from ReText.editor import ReTextEdit
from ReText.highlighter import ReTextHighlighter
from ReText.syncscroll import SyncScroll

try:
	from ReText.fakevimeditor import ReTextFakeVimHandler
except ImportError:
	ReTextFakeVimHandler = None

from PyQt5.QtCore import pyqtSignal, Qt, QDir, QFile, QFileInfo, QObject, QPoint, QTextStream, QTimer, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QTextBrowser, QTextEdit, QSplitter
from PyQt5.QtWebKit import QWebSettings
from PyQt5.QtWebKitWidgets import QWebPage, QWebView

PreviewDisabled, PreviewLive, PreviewNormal = range(3)

class ReTextTab(QObject):

	fileNameChanged = pyqtSignal()
	modificationStateChanged = pyqtSignal()
	activeMarkupChanged = pyqtSignal()

	# Make _fileName a read-only property to make sure that any
	# modification happens through the proper functions. These functions
	# will make sure that the fileNameChanged signal is emitted when
	# applicable.
	@property
	def fileName(self):
		return self._fileName

	def __init__(self, parent, fileName, defaultMarkup, previewState=PreviewDisabled):
		QObject.__init__(self, parent)
		self.p = parent
		self._fileName = fileName
		self.editBox = ReTextEdit(self)
		self.previewBox = self.createPreviewBox(self.editBox)
		self.defaultMarkupClass = defaultMarkup
		self.activeMarkupClass = None
		self.markup = None
		self.previewState = previewState
		self.previewBlocked = False

		textDocument = self.editBox.document()
		self.highlighter = ReTextHighlighter(textDocument)
		if enchant_available and parent.actionEnableSC.isChecked():
			self.highlighter.dictionary = enchant.Dict(parent.sl or None)
			# Rehighlighting is tied to the change in markup class that
			# happens at the end of this function

		self.editBox.textChanged.connect(self.updateLivePreviewBox)
		self.editBox.undoAvailable.connect(parent.actionUndo.setEnabled)
		self.editBox.redoAvailable.connect(parent.actionRedo.setEnabled)
		self.editBox.copyAvailable.connect(parent.actionCopy.setEnabled)

		textDocument.modificationChanged.connect(self.handleModificationChanged)

		self.updateActiveMarkupClass()
		self.updateBoxesVisibility()

	def handleModificationChanged(self):
		self.modificationStateChanged.emit()

	def editorPositionToSourceLine(self, editorPosition):
		viewportPosition = editorPosition - self.editBox.verticalScrollBar().value()
		sourceLine = self.editBox.cursorForPosition(QPoint(0,viewportPosition)).blockNumber()
		return sourceLine

	def sourceLineToEditorPosition(self, sourceLine):
		doc = self.editBox.document()
		block = doc.findBlockByNumber(sourceLine)
		rect = doc.documentLayout().blockBoundingRect(block)
		return rect.top()

	def createPreviewBox(self, editBox):
		if globalSettings.useWebKit:
			preview = ReTextWebPreview(editBox,
			                           self.editorPositionToSourceLine,
			                           self.sourceLineToEditorPosition)
		else:
			preview = ReTextPreview(self)

		return preview

	def getSplitter(self):
		splitter = QSplitter(Qt.Horizontal)
		# Give both boxes a minimum size so the minimumSizeHint will be
		# ignored when splitter.setSizes is called below
		for widget in self.editBox, self.previewBox:
			widget.setMinimumWidth(125)
			splitter.addWidget(widget)
		splitter.setSizes((50, 50))
		splitter.setChildrenCollapsible(False)
		splitter.tab = self
		return splitter

	def setDefaultMarkupClass(self, markupClass):
		'''
		Set the default markup class to use in case a markup that
		matches the filename cannot be found. This function calls
		updateActiveMarkupClass so it can decide if the active 
		markup class also has to change.
		'''
		self.defaultMarkupClass = markupClass
		self.updateActiveMarkupClass()

	def getActiveMarkupClass(self):
		'''
		Return the currently active markup class for this tab.
		No objects should be created of this class, it should
		only be used to retrieve markup class specific information.
		'''
		return self.activeMarkupClass

	def updateActiveMarkupClass(self):
		'''
		Update the active markup class based on the default class and
		the current filename. If the active markup class changes, the
		highlighter is rerun on the input text, the markup object of
		this tab is replaced with one of the new class and the
		activeMarkupChanged signal is emitted.
		'''
		previousMarkupClass = self.activeMarkupClass

		self.activeMarkupClass = self.defaultMarkupClass

		if self._fileName:
			markupClass = get_markup_for_file_name(
				self._fileName, return_class=True)
			if markupClass:
				self.activeMarkupClass = markupClass

		if self.activeMarkupClass != previousMarkupClass:
			self.highlighter.docType = self.activeMarkupClass.name if self.activeMarkupClass else None
			self.highlighter.rehighlight()

			# for now create a markup object here
			self.markup = self.getMarkup()

			# TODO: trigger a preview update here?

			self.activeMarkupChanged.emit()

	def getMarkup(self):
		markupClass = self.getActiveMarkupClass()
		if markupClass and markupClass.available():
			return markupClass(filename=self._fileName)
		return None

	def getDocumentTitle(self, baseName=False):
		if self.markup and not baseName:
			text = self.editBox.toPlainText()
			try:
				return self.markup.get_document_title(text)
			except Exception:
				self.p.printError()
		if self._fileName:
			fileinfo = QFileInfo(self._fileName)
			basename = fileinfo.completeBaseName()
			return (basename if basename else fileinfo.fileName())
		return self.tr("New document")

	def getHtml(self, includeStyleSheet=True, webenv=False, syncScroll=False):
		if self.markup is None:
			markupClass = self.getActiveMarkupClass()
			errMsg = self.tr('Could not parse file contents, check if '
			                 'you have the <a href="%s">necessary module</a> '
			                 'installed!')
			try:
				errMsg %= markupClass.attributes[MODULE_HOME_PAGE]
			except (AttributeError, KeyError):
				# Remove the link if markupClass doesn't have the needed attribute
				errMsg = errMsg.replace('<a href="%s">', '').replace('</a>', '')
			return '<p style="color: red">%s</p>' % errMsg
		text = self.editBox.toPlainText()
		headers = ''
		if includeStyleSheet:
			headers += '<style type="text/css">\n' + self.p.ss + '</style>\n'
		baseName = self.getDocumentTitle(baseName=True)
		cssFileName = baseName + '.css'
		if QFile.exists(cssFileName):
			headers += ('<link rel="stylesheet" type="text/css" href="%s">\n'
			% cssFileName)
		headers += ('<meta name="generator" content="ReText %s">\n' % app_version)
		self.markup.requested_extensions = []
		if syncScroll:
			self.markup.requested_extensions.append('ReText.mdx_posmap')
		return self.markup.get_whole_html(text,
			custom_headers=headers, include_stylesheet=includeStyleSheet,
			fallback_title=baseName, webenv=webenv)

	def updatePreviewBox(self):
		self.previewBlocked = False
		if isinstance(self.previewBox, QTextEdit):
			scrollbar = self.previewBox.verticalScrollBar()
			scrollbarValue = scrollbar.value()
			distToBottom = scrollbar.maximum() - scrollbarValue
		try:
			html = self.getHtml(syncScroll=globalSettings.syncScroll)
		except Exception:
			return self.p.printError()
		if isinstance(self.previewBox, QTextEdit):
			self.previewBox.setHtml(html)
			self.previewBox.document().setDefaultFont(globalSettings.font)
			# If scrollbar was at bottom (and that was not the same as top),
			# set it to bottom again
			if scrollbarValue:
				newValue = scrollbar.maximum() - distToBottom
				scrollbar.setValue(newValue)
		else:
			settings = self.previewBox.settings()
			settings.setFontFamily(QWebSettings.StandardFont,
			                       globalSettings.font.family())
			settings.setFontSize(QWebSettings.DefaultFontSize,
			                     globalSettings.font.pointSize())
			self.previewBox.setHtml(html, QUrl.fromLocalFile(self._fileName))

	def updateLivePreviewBox(self):
		if self.previewState == PreviewLive and not self.previewBlocked:
			self.previewBlocked = True
			QTimer.singleShot(1000, self.updatePreviewBox)

	def updateBoxesVisibility(self):
		self.editBox.setVisible(self.previewState < PreviewNormal)
		self.previewBox.setVisible(self.previewState > PreviewDisabled)

	def readTextFromFile(self, fileName=None, encoding=None):
		previousFileName = self._fileName
		if fileName:
			self._fileName = fileName
		openfile = QFile(self._fileName)
		openfile.open(QFile.ReadOnly)
		stream = QTextStream(openfile)
		encoding = encoding or globalSettings.defaultCodec
		if encoding:
			stream.setCodec(encoding)
		text = stream.readAll()
		openfile.close()

		modified = bool(encoding) and (self.editBox.toPlainText() != text)
		self.editBox.setPlainText(text)
		self.editBox.document().setModified(modified)

		if previousFileName != self._fileName:
			self.updateActiveMarkupClass()
			self.fileNameChanged.emit()


	def saveTextToFile(self, fileName=None, addToWatcher=True):
		previousFileName = self._fileName
		if fileName:
			self._fileName = fileName
		self.p.fileSystemWatcher.removePath(previousFileName)
		savefile = QFile(self._fileName)
		result = savefile.open(QFile.WriteOnly)
		if result:
			savestream = QTextStream(savefile)
			if globalSettings.defaultCodec:
				savestream.setCodec(globalSettings.defaultCodec)
			savestream << self.editBox.toPlainText()
			savefile.close()
			self.editBox.document().setModified(False)
		if result and addToWatcher:
			self.p.fileSystemWatcher.addPath(self._fileName)

		if previousFileName != self._fileName:
			self.updateActiveMarkupClass()
			self.fileNameChanged.emit()

		return result

	def installFakeVimHandler(self):
		if ReTextFakeVimHandler:
			fakeVimEditor = ReTextFakeVimHandler(self.editBox, self)
			fakeVimEditor.setSaveAction(self.actionSave)
			fakeVimEditor.setQuitAction(self.actionQuit)
			# TODO: action is bool, really call remove?
			self.p.actionFakeVimMode.triggered.connect(fakeVimEditor.remove)

class ReTextWebPreview(QWebView):

	def __init__(self, editBox,
	             editorPositionToSourceLineFunc,
	             sourceLineToEditorPositionFunc):

		QWebView.__init__(self)

		self.editBox = editBox

		if not globalSettings.handleWebLinks:
			self.page().setLinkDelegationPolicy(QWebPage.DelegateExternalLinks)
			self.page().linkClicked.connect(QDesktopServices.openUrl)
		self.settings().setAttribute(QWebSettings.LocalContentCanAccessFileUrls, False)
		self.settings().setDefaultTextEncoding('utf-8')
		# Avoid caching of CSS
		self.settings().setObjectCacheCapacities(0,0,0)

		self.syncscroll = SyncScroll(self.page().mainFrame(),
					     editorPositionToSourceLineFunc,
					     sourceLineToEditorPositionFunc)

		# Events relevant to sync scrolling
		self.editBox.cursorPositionChanged.connect(self._handleCursorPositionChanged)
		self.editBox.verticalScrollBar().valueChanged.connect(self.syncscroll.handleEditorScrolled)
		self.editBox.resized.connect(self._handleEditorResized)

		# Scroll the preview when the mouse wheel is used to scroll
		# beyond the beginning/end of the editor
		self.editBox.scrollLimitReached.connect(self._handleWheelEvent)

	def disconnectExternalSignals(self):
		self.editBox.cursorPositionChanged.disconnect(self._handleCursorPositionChanged)
		self.editBox.verticalScrollBar().valueChanged.disconnect(self.syncscroll.handleEditorScrolled)
		self.editBox.resized.disconnect(self._handleEditorResized)

		self.editBox.scrollLimitReached.disconnect(self._handleWheelEvent)

	def _handleWheelEvent(self, event):
		"""
		Use this intermediate function because it is not possible to
		disconnect a built-in method. It would generate the following error:
		  TypeError: 'builtin_function_or_method' object is not connected
		"""
		# Only pass wheelEvents on to the preview if syncscroll is
		# controlling the position of the preview
		if self.syncscroll.isActive():
			self.wheelEvent(event)

	def _handleCursorPositionChanged(self):
		editorCursorPosition = self.editBox.verticalScrollBar().value() + \
				       self.editBox.cursorRect().top()
		self.syncscroll.handleCursorPositionChanged(editorCursorPosition)

	def _handleEditorResized(self, rect):
		self.syncscroll.handleEditorResized(rect.height())


class ReTextPreview(QTextBrowser):
	"""
	When links like [test](test) are clicked, the file test.md is opened.
	It has to be located next to the current opened file.
	Relative pathes like [test](../test) or [test](folder/test) are also possible.
	"""

	def __init__(self, tab):
		QTextBrowser.__init__(self)
		self.tab = tab
		# if set to True, links to other files will unsuccessfully be opened as anchors
		self.setOpenLinks(False)
		self.anchorClicked.connect(self.openInternal)

	def disconnectExternalSignals(self):
		pass

	def openInternal(self, link):
		url = link.url()
		isLocalHtml = (link.scheme() in ('file', '') and url.endswith('.html'))
		if url.startswith('#'):
			self.scrollToAnchor(url[1:])
		elif link.isRelative() and get_markup_for_file_name(url, return_class=True):
			fileToOpen = QDir.current().filePath(url)
			if not QFileInfo(fileToOpen).completeSuffix() and self._fileName:
				fileToOpen += '.' + QFileInfo(self.tab.fileName).completeSuffix()
			self.tab.p.openFileWrapper(fileToOpen)
		elif globalSettings.handleWebLinks and isLocalHtml:
			self.setSource(link)
		else:
			QDesktopServices.openUrl(link)
