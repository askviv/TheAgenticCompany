import unittest
from unittest.mock import Mock, patch

from servers.company.inventory import inventory, link_plane


class InventoryTests(unittest.TestCase):
    def test_links_only_explicit_username_and_keeps_missing_storage_unresolved(self):
        chat=Mock()
        chat._request.return_value={'users':[
            {'_id':'real-sarah-id','username':'sarah_johnson','name':'Sarah Johnson','emails':[{'address':'sarah@example.com'}]},
            {'_id':'different-account','username':'sarah2','name':'Sarah Johnson','emails':[{'address':'sarah@example.com'}]}]}
        response=Mock()
        response.json.return_value={'ocs':{'meta':{'statuscode':100},'data':{'users':['theagentcompany']}}}
        with patch('servers.company.inventory.requests.get',return_value=response) as get:
            result=inventory({'Sarah Johnson':{'username':'sarah_johnson'}},chat,'http://storage',('operator','private'))
        employee=result['employees'][0]
        self.assertEqual(employee['accounts']['rocketchat']['id'],'real-sarah-id')
        self.assertIsNone(employee['accounts']['owncloud'])
        self.assertEqual(employee['unresolved'],['No seeded individual ownCloud account'])
        self.assertNotIn('private',str(result))
        self.assertEqual(chat._request.call_args.args[:2],('GET','users.list'))
        get.assert_called_once()

    def test_rejected_storage_discovery_does_not_report_empty_success(self):
        chat=Mock();chat._request.return_value={'users':[]}
        response=Mock();response.json.return_value={'ocs':{'meta':{'statuscode':997},'data':{}}}
        with patch('servers.company.inventory.requests.get',return_value=response):
            with self.assertRaisesRegex(RuntimeError,'discovery rejected'):
                inventory({},chat,'http://storage',('operator','private'))

    def test_plane_link_uses_account_id_instead_of_membership_and_reports_unmapped(self):
        employees=[{'id':'sarah-johnson','accounts':{},'unresolved':[]},
                   {'id':'new-person','accounts':{},'unresolved':[]}]
        members=[{'id':'membership-uuid','role':20,'member':{'id':'account-uuid','email':'sarah.johnson@agentcompany.com'}}]
        link_plane(employees,members,{'sarah-johnson':'sarah.johnson@agentcompany.com'})
        self.assertEqual(employees[0]['accounts']['plane']['id'],'account-uuid')
        self.assertEqual(employees[0]['accounts']['plane']['membership_id'],'membership-uuid')
        self.assertNotIn('plane',employees[1]['accounts'])
        self.assertEqual(employees[1]['unresolved'],['Plane mapping not configured'])


if __name__=='__main__': unittest.main()
