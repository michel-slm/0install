import gtk, gobject, os
from zeroinstall.injector import model, writer
from gui import policy, pretty_size

def popup_menu(bev, values, fn):
	menu = gtk.Menu()
	for value in values:
		if value is None:
			item = gtk.SeparatorMenuItem()
		else:
			item = gtk.MenuItem(str(value).capitalize())
			item.connect('activate', lambda item, v=value: fn(v))
		item.show()
		menu.append(item)
	menu.popup(None, None, None, bev.button, bev.time)

rox_filer = 'http://rox.sourceforge.net/2005/interfaces/ROX-Filer'

# Columns
ITEM = 0
ARCH = 1
STABILITY = 2
VERSION = 3
CACHED = 4
UNUSABLE = 5

class ImplementationList(gtk.ScrolledWindow):
	tree_view = None
	model = None

	def __init__(self, interface):
		gtk.ScrolledWindow.__init__(self, None, None)
		self.set_shadow_type(gtk.SHADOW_IN)

		self.model = gtk.ListStore(object, str, str, str,
			   gobject.TYPE_BOOLEAN, gobject.TYPE_BOOLEAN)

		self.tree_view = gtk.TreeView(self.model)

		text = gtk.CellRendererText()
		text_strike = gtk.CellRendererText()
		toggle = gtk.CellRendererToggle()

		stability = gtk.TreeViewColumn('Stability', text, text = STABILITY)

		for column in (gtk.TreeViewColumn('Version', text, text = VERSION, strikethrough = UNUSABLE),
			       stability,
			       gtk.TreeViewColumn('C', toggle, active = CACHED),
			       gtk.TreeViewColumn('Arch', text, text = ARCH)):
			self.tree_view.append_column(column)

		self.add(self.tree_view)

		def button_press(tree_view, bev):
			if bev.button not in (1, 3):
				return False
			pos = tree_view.get_path_at_pos(int(bev.x), int(bev.y))
			if not pos:
				return False
			path, col, x, y = pos
			impl = self.model[path][ITEM]
			if col == stability:
				upstream = impl.upstream_stability or model.testing
				choices = model.stability_levels.values()
				choices.sort()
				choices.reverse()
				def set(new):
					if isinstance(new, model.Stability):
						impl.user_stability = new
					else:
						impl.user_stability = None
					writer.save_interface(interface)
					policy.recalculate()
				popup_menu(bev, ['Unset (%s)' % upstream, None] + choices,
					set)
			elif bev.button == 3 and policy.get_cached(impl):
				def open(item):
					os.spawnlp(os.P_WAIT, '0launch',
						'0launch', rox_filer, '-d',
						policy.get_implementation_path(impl))
				popup_menu(bev, ['Open cached copy'], open)
		self.tree_view.connect('button-press-event', button_press)
	
	def get_selection(self):
		return self.tree_view.get_selection()
	
	def set_items(self, items):
		self.model.clear()
		for item in items:
			new = self.model.append()
			self.model[new][ITEM] = item
			self.model[new][VERSION] = item.get_version()
			self.model[new][CACHED] = policy.get_cached(item)
			if item.user_stability:
				self.model[new][STABILITY] = str(item.user_stability).upper()
			else:
				self.model[new][STABILITY] = item.upstream_stability or \
							     model.testing
			self.model[new][ARCH] = item.arch or 'any'
			self.model[new][UNUSABLE] = policy.is_unusable(item)
	
	def clear(self):
		self.model.clear()
