# Copyright (c) 2013, Web Notes Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals
import webnotes

from webnotes.utils import cstr, flt, nowdate
from webnotes.model.code import get_obj
from webnotes import msgprint, _

class OverProductionError(webnotes.ValidationError): pass

class DocType:
	def __init__(self, doc, doclist=[]):
		self.doc = doc
		self.doclist = doclist

	def validate(self):
		if self.doc.docstatus == 0:
			self.doc.status = "Draft"
			
		import utilities
		utilities.validate_status(self.doc.status, ["Draft", "Submitted", "Stopped", 
			"In Process", "Completed", "Cancelled"])

		self.validate_bom_no()
		self.validate_sales_order()
		self.validate_warehouse()
		
		from utilities.transaction_base import validate_uom_is_integer
		validate_uom_is_integer(self.doclist, "stock_uom", ["qty", "produced_qty"])
		
	def validate_bom_no(self):
		if self.doc.bom_no:
			bom = webnotes.conn.sql("""select name from `tabBOM` where name=%s and docstatus=1 
				and is_active=1 and item=%s"""
				, (self.doc.bom_no, self.doc.production_item), as_dict =1)
			if not bom:
				webnotes.throw("""Incorrect BOM: %s entered. 
					May be BOM not exists or inactive or not submitted 
					or for some other item.""" % cstr(self.doc.bom_no))
					
	def validate_sales_order(self):
		if self.doc.sales_order:
			so = webnotes.conn.sql("""select name, delivery_date from `tabSales Order` 
				where name=%s and docstatus = 1""", self.doc.sales_order, as_dict=1)[0]

			if not so.name:
				webnotes.throw("Sales Order: %s is not valid" % self.doc.sales_order)

			if not self.doc.expected_delivery_date:
				self.doc.expected_delivery_date = so.delivery_date
			
			self.validate_production_order_against_so()
			
	def validate_warehouse(self):
		from stock.utils import validate_warehouse_user, validate_warehouse_company
		
		for w in [self.doc.fg_warehouse, self.doc.wip_warehouse]:
			validate_warehouse_user(w)
			validate_warehouse_company(w, self.doc.company)
	
	def validate_production_order_against_so(self):
		# already ordered qty
		ordered_qty_against_so = webnotes.conn.sql("""select sum(qty) from `tabProduction Order`
			where production_item = %s and sales_order = %s and docstatus < 2 and name != %s""", 
			(self.doc.production_item, self.doc.sales_order, self.doc.name))[0][0]

		total_qty = flt(ordered_qty_against_so) + flt(self.doc.qty)
		
		# get qty from Sales Order Item table
		so_item_qty = webnotes.conn.sql("""select sum(qty) from `tabSales Order Item` 
			where parent = %s and item_code = %s""", 
			(self.doc.sales_order, self.doc.production_item))[0][0]
		# get qty from Packing Item table
		dnpi_qty = webnotes.conn.sql("""select sum(qty) from `tabPacked Item` 
			where parent = %s and parenttype = 'Sales Order' and item_code = %s""", 
			(self.doc.sales_order, self.doc.production_item))[0][0]
		# total qty in SO
		so_qty = flt(so_item_qty) + flt(dnpi_qty)
				
		if total_qty > so_qty:
			webnotes.throw(_("Total production order qty for item") + ": " + 
				cstr(self.doc.production_item) + _(" against sales order") + ": " + 
				cstr(self.doc.sales_order) + _(" will be ") + cstr(total_qty) + ", " + 
				_("which is greater than sales order qty ") + "(" + cstr(so_qty) + ")" + 
				_("Please reduce qty."), exc=OverProductionError)

	def stop_unstop(self, status):
		""" Called from client side on Stop/Unstop event"""
		self.update_status(status)
		qty = (flt(self.doc.qty)-flt(self.doc.produced_qty)) * ((status == 'Stopped') and -1 or 1)
		self.update_planned_qty(qty)
		msgprint("Production Order has been %s" % status)


	def update_status(self, status):
		if status == 'Stopped':
			webnotes.conn.set(self.doc, 'status', cstr(status))
		else:
			if flt(self.doc.qty) == flt(self.doc.produced_qty):
				webnotes.conn.set(self.doc, 'status', 'Completed')
			if flt(self.doc.qty) > flt(self.doc.produced_qty):
				webnotes.conn.set(self.doc, 'status', 'In Process')
			if flt(self.doc.produced_qty) == 0:
				webnotes.conn.set(self.doc, 'status', 'Submitted')


	def on_submit(self):
		if not self.doc.wip_warehouse:
			webnotes.throw(_("WIP Warehouse required before Submit"))
		webnotes.conn.set(self.doc,'status', 'Submitted')
		self.update_planned_qty(self.doc.qty)
		

	def on_cancel(self):
		# Check whether any stock entry exists against this Production Order
		stock_entry = webnotes.conn.sql("""select name from `tabStock Entry` 
			where production_order = %s and docstatus = 1""", self.doc.name)
		if stock_entry:
			webnotes.throw("""Submitted Stock Entry %s exists against this production order. 
				Hence can not be cancelled.""" % stock_entry[0][0])

		webnotes.conn.set(self.doc,'status', 'Cancelled')
		self.update_planned_qty(-self.doc.qty)

	def update_planned_qty(self, qty):
		"""update planned qty in bin"""
		args = {
			"item_code": self.doc.production_item,
			"warehouse": self.doc.fg_warehouse,
			"posting_date": nowdate(),
			"planned_qty": flt(qty)
		}
		from stock.utils import update_bin
		update_bin(args)

@webnotes.whitelist()	
def get_item_details(item):
	res = webnotes.conn.sql("""select stock_uom, description
		from `tabItem` where (ifnull(end_of_life, "")="" or end_of_life > now())
		and name=%s""", item, as_dict=1)
	
	if not res:
		return {}
		
	res = res[0]
	bom = webnotes.conn.sql("""select name from `tabBOM` where item=%s 
		and ifnull(is_default, 0)=1""", item)
	if bom:
		res.bom_no = bom[0][0]
		
	return res

@webnotes.whitelist()
def make_stock_entry(production_order_id, purpose):
	production_order = webnotes.bean("Production Order", production_order_id)
		
	stock_entry = webnotes.new_bean("Stock Entry")
	stock_entry.doc.purpose = purpose
	stock_entry.doc.production_order = production_order_id
	stock_entry.doc.company = production_order.doc.company
	stock_entry.doc.bom_no = production_order.doc.bom_no
	stock_entry.doc.use_multi_level_bom = production_order.doc.use_multi_level_bom
	stock_entry.doc.fg_completed_qty = flt(production_order.doc.qty) - flt(production_order.doc.produced_qty)
	
	if purpose=="Material Transfer":
		stock_entry.doc.to_warehouse = production_order.doc.wip_warehouse
	else:
		stock_entry.doc.from_warehouse = production_order.doc.wip_warehouse
		stock_entry.doc.to_warehouse = production_order.doc.fg_warehouse
		
	stock_entry.run_method("get_items")
	return [d.fields for d in stock_entry.doclist]



@webnotes.whitelist()
def get_details():
	webnotes.errprint("user")
	webnotes.errprint(webnotes.session['user'])
	usr=webnotes.session['user']
	pr=webnotes.conn.sql("select account_id from tabProfile where name='"+usr+"' and franchise_admin='1'")
	if pr:
		rs="select name,password from tabFranchise where name='"+pr[0][0]+"'"
		webnotes.errprint(rs)
		res=webnotes.conn.sql(rs)
		#return "testaccount","123456"
		webnotes.errprint(res)
        	if res:
		   return res
		else:
		   return "Not"
	else:
	   return "Not"

@webnotes.whitelist()
def get_detail1(serial_no=None):
	if serial_no:
		webnotes.errprint(serial_no)
		txt = serial_no
		data=webnotes.conn.sql("select case when status='Delivered' then customer else warehouse end as data,status from `tabSerial No` where name='%s'"%(txt),as_dict=1)
		webnotes.errprint(data)
		if data :
			if data[0]['status']=='Delivered':
				return 'customer',data[0]['data']
			elif data[0]['status']=='Available' and data[0]['data']=='Finished Goods - P':
				return 'admin',data[0]['data']
			else:
				
				rs=webnotes.conn.sql("select name, password from tabFranchise where name='"+data[0]['data']+"'",as_list=1)
				device_id = webnotes.conn.sql("""select name from tabVehicle where account_id = '%s'"""%data[0]['data'],as_list=1)
           		return 'usr',rs,device_id[0][0]
    		else:
			return 'Not'
	else :
		return "Not"
