# -*- coding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution
#    Copyright (C) 2016 luca Vercelli
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from odoo import models,fields,api
from odoo.exceptions import UserError

#see /odoo/odoo-server/addons/product/models/product.py

import os
import logging

from . import util

_logger = logging.getLogger(__name__)

class WizardExportInvoices(models.TransientModel):
    _name = "exportts.wizard.export"
    _description = "Esporta fatture in XML"

    proprietario_id = fields.Many2one('res.partner',string='Proprietario')

    #@api.multi
    def export(self):
        """
        I can export many invoices, neverthless I get a single Export object
        """
        now = fields.Datetime.now()
 
        invoices = self.env['account.invoice'].browse(self.env.context['active_ids'])
        companies = [i.number for i in invoices if i.partner_id.is_company]
        oppositions = [i.number for i in invoices if i.partner_id.opposizione_730]
        messages = ""
        if companies:
            messages = messages + "Fatture ignorate perchè non intestate a persone fisiche: " + str(companies) + "\r\n"
        if oppositions:
            messages = messages + "Fatture con opposizione alla dichiarazione TS: " + str(oppositions) + "\r\n"
        
        ctx = self.env.context
        values = {
            'doc_ids' : ctx['active_ids'],
            'doc_model' : ctx['active_model'],
            'docs' : self.env[ctx['active_model']].browse(ctx['active_ids']),
            'proprietario' : self.proprietario_id
        }

        result = self.env['ir.actions.report'].render_template('l10n_it_export_ts.qweb_invoice_xml_ts', values)

        self.env['exportts.export.registry'].create({
            'proprietario_id' : self.proprietario_id.id,
            'status' : 'Exported',
            'xml' : result,
            'date_export' : now,
            'messages': messages
            })


# <<Il trattamento e la conservazione del codice fiscale dell'assistito,
#rilevato dalla Tessera Sanitaria, crittografato secondo le
#modalita' di cui al decreto attuativo del comma 5 dell'articolo 50
#del DL 269/2003, utilizzando la chiave pubblica RSA contenuta
#nel certificato X.509 fornito dal sistema TS ed applicando il
#padding PKCS#1 v 1.5. Tale trattamento deve essere eseguito
#tramite procedure automatizzate all'atto della memorizzazione
#negli archivi locali.>>

# i due indirizzi sono questi:
# URL_TEST="https://invioss730ptest.sanita.finanze.it/InvioTelematicoSS730pMtomWeb/InvioTelematicoSS730pMtomPort"
# URL_PROD="https://invioss730p.sanita.finanze.it/InvioTelematicoSS730pMtomWeb/InvioTelematicoSS730pMtomPort"
# il problema e' che ?wsdl *non* viene reso disponibile via web
# quindi abbiamo i due file WSDL in locale:

import os
PARENT_FOLDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FOLDER = os.path.join(PARENT_FOLDER, "data")

XSD_FILENAME = os.path.join(DATA_FOLDER, "730_precompilata.xsd")
WSDL_PROD = os.path.join(DATA_FOLDER, "InvioTelematicoSpeseSanitarie730p.wsdl")
WSDL_TEST = os.path.join(DATA_FOLDER, "InvioTelematicoSpeseSanitarie730pTest.wsdl")
WSDL_ESITO = os.path.join(DATA_FOLDER, "EsitoInvioDatiSpesa730Service.wsdl")
WSDL_DET_ERRORI = os.path.join(DATA_FOLDER, "DettaglioErrori730Service.wsdl")
WSDL_RICEVUTE = os.path.join(DATA_FOLDER, "RicevutaPdf730Service.wsdl")



class WizardSendToTS(models.TransientModel):
    _name = "exportts.wizard.send"
    _description = "Invia XML a Sistema TS"

    pincode_inviante = fields.Char('PINCODE inviante', required=True)
    password_inviante = fields.Char('Password', required=True)
    endpoint = fields.Selection([('P','Produzione'),('T','Test')], required=True)
    folder = fields.Char('Backup Directory', help='Absolute path for storing files', required='True',
                         default='/odoo/backups/sistemats')

    #@api.one
    def send(self):
        if not os.path.exists(self.folder):
            raise UserError("Folder " + self.folder + " does not exist!")
        export = self.env['exportts.export.registry'].browse(self.env.context['active_id'])
        self.cf_proprietario = export.proprietario_id.fiscalcode
        self.cf_proprietario_enc = export.proprietario_id.fiscalcode_enc
        self.p_iva = export.proprietario_id.vat
        self.pincode_inviante_enc = util.encrypt(self.pincode_inviante)
        self.xmlfilename = util.write_to_new_tempfile(export.xml, prefix='invoices', suffix='.xml')
        self.use_test_url = (self.endpoint == 'T')

        #chdir because I need to find the schema file
        os.chdir(DATA_FOLDER)
        _logger.info("Now changed dir to %s", os.getcwd())

        _logger.info("Validating...")
        
        util.test_xsd(self.xmlfilename, XSD_FILENAME)
        _logger.info("Compressione dati...")
        self.zipfilename = util.zip_single_file(self.xmlfilename)
        
        _logger.info("Invio dati...")
        answer = self.call_ws_invio()
        export.status = "sent"

        _logger.info("Invio concluso. Risposta:")
        _logger.info(answer)

        if answer.protocollo:
            self.protocollo = answer.protocollo
            
            import time
            time.sleep(4)
            _logger.info("Esito invio:")
            answer2 = self.call_ws_esito()
            _logger.info(answer2)
            if answer2.esitiPositivi and answer2.esitiPositivi.dettagliEsito:
                dettagli = answer2.esitiPositivi.dettagliEsito[0]
                if dettagli.nAccolti == 0:
                    export.status = "Rejected"
                elif dettagli.nInviati == dettagli.nAccolti:
                    export.status = "Accepted"
                elif dettagli.nWarnings > 0:
                    export.status = "Accepted with warnings"
                else:
                    export.status = "Some rejected"
            
            answer3, pdf_filename = self.call_ws_ricevuta()
            _logger.info("Ricevuta PDF salvata in: %s", pdf_filename)
            export.pdf_filename = pdf_filename
            
            answer4, csv_filename = self.call_ws_dettaglio_errori()
            _logger.info("Dettaglio errori CSV salvato in: %s", csv_filename)
            export.csv_filename = csv_filename

    def _create_transport(self):
        """
        Create a new Transport with HTTP Authentication
        as required by all webservices
        """
        from requests import Session
        from requests.auth import HTTPBasicAuth
        from zeep.transports import Transport
        
        session = Session()
        session.auth = HTTPBasicAuth(self.cf_proprietario, self.password_inviante)
        return Transport(session=session)
        
    def call_ws_invio(self):
        """
        Call the webservice "inviaFileMtom()".
        Fill all required parameters: file name, file content, owner data, basic auth. credentials
        Currently, require a modified version of osa (as we need Basic Authentication)
        Currently, MTOM protocol is not used, file is sent as part of the message.
        
        @return webservice answer, which is an object of type "inviaFileMtomResponse"
        """
        from zeep import Client
        
        wsdl = WSDL_TEST if self.use_test_url else WSDL_PROD
        
        client = Client(wsdl=wsdl, transport=self._create_transport())
        
        documento = open(self.zipfilename, "rb").read()

        return client.service.inviaFileMtom(
                nomeFileAllegato = os.path.basename(self.zipfilename),
                pincodeInvianteCifrato = self.pincode_inviante_enc,
                datiProprietario = {
                        'cfProprietario' : self.cf_proprietario    #cleartext
                    },
                documento = documento
                )

    #Questa era una delle risposte:
    #(ricevutaInvio){
    #    codiceEsito = 000
    #    dataAccoglienza = 25-12-2016 22:24:20
    #    descrizioneEsito = Il file  in attesa di elaborazione, per conoscerne l'esito  necessario verificare la ricevuta
    #    dimensioneFileAllegato = 24922
    #    nomeFileAllegato = file1.xmlI4c52M.zip
    #    protocollo = 16122522242096203
    #    idErrore = 
    #}

    def call_ws_esito(self):
        """
        Call the webservice "EsitoInvii()".
        Restituisce l'esito dell'invio corrispondente al numero di protocollo dato
        @return webservice answer
        """
        from zeep import Client

        client = Client(wsdl=WSDL_ESITO, transport=self._create_transport())

        #alternativi al protocollo:
        #DatiInputRichiesta.dataInizio = '24-12-2016'
        #DatiInputRichiesta.dataFine = '26-12-2016'

        return client.service.EsitoInvii(DatiInputRichiesta = {
                'pinCode' : self.pincode_inviante_enc,
                'protocollo' : self.protocollo
            })

    #Questa era una delle risposte:
    #(datiOutput){
    #    esitoChiamata = 0
    #    descrizioneEsito =  
    #    esitiPositivi = (esitiPositivi){
    #                        dettagliEsito[] = [
    #                                           (dettaglioEsitoPositivo){
    #                                               protocollo = 16122522242096203
    #                                               dataInvio = 25-12-2016 22:24:20
    #                                               stato = 5
    #                                               descrizione = File scartato in fase di Elaborazione
    #                                               nInviati = 0
    #                                               nAccolti = 0
    #                                               nWarnings = 0
    #                                               nErrori = 0
    #                                           }
    #                                           ]
    #                    }
    #    esitiNegativi = None (esitiNegativi)
    #}

    def call_ws_dettaglio_errori(self):
        """
        Call the webservice "DettaglioErrori()".
        Restituisce un CSV contenente il dettaglio degli errori di importazione
        @return (webservice answer, csv_filename)
        """
        from zeep import Client

        client = Client(wsdl=WSDL_DET_ERRORI, transport=self._create_transport())

        answer = client.service.DettaglioErrori(DatiInputRichiesta = {
                'pinCode' : self.pincode_inviante_enc,
                'protocollo' : self.protocollo
            })
        _logger.info(answer)
        csv_filename = None
        try:
            if answer.esitiPositivi.dettagliEsito.csv:
                csv_filename = util.write_to_new_tempfile(answer.esitiPositivi.dettagliEsito.csv,
                        prefix="errori", suffix=".csv.zip", dir=self.folder)
        except:
            pass
        return (answer, csv_filename)

    #Questa era una delle risposte:
    #(datiOutput){
    #    esitoChiamata = 1
    #    esitiPositivi = None (esitiPositivi)
    #    esitiNegativi = (esitiNegativi){
    #                        dettaglioEsitoNegativo[] = [
    #                                                    (dettaglioEsitoNegativo){
    #                                                        codice = WS37
    #                                                        descrizione = FILE SCARTATO, CONTROLLARE LA RICEVUTA PDF
    #                                                    }
    #                                                    ]
    #                    }
    #}

    def call_ws_ricevuta(self):
        """
        Call the webservice "RicevutaPdf()".
        Restituisce la ricevuta dell'invio corrispondente al numero di protocollo dato
        @return (webservice answer, local PDF temp file)
        """
        from zeep import Client

        client = Client(wsdl=WSDL_RICEVUTE, transport=self._create_transport())

        answer = client.service.RicevutaPdf(DatiInputRichiesta = {
                'pinCode' : self.pincode_inviante_enc,
                'protocollo' : self.protocollo
            })

        _logger.info(answer)
        pdf_filename = None
        try:
            if answer.esitiPositivi and answer.esitiPositivi.dettagliEsito and answer.esitiPositivi.dettagliEsito.pdf:
                pdf_filename = util.write_to_new_tempfile(answer.esitiPositivi.dettagliEsito.pdf,
                                        prefix="ricevuta", suffix=".pdf", dir=self.folder)
        except:
            pass
        return (answer, pdf_filename)


class WizardEncryptAllFiscalCodes(models.TransientModel):
    _name = "res.partner.encrypt"
    _description = "Encrypt fiscal codes"

    #@api.one
    def encrypt_all_fiscalcodes(self):
        """
        This encrypts all fiscalcode on demand.
        """
        model = self.env['res.partner']
        all_partners = model.search([])
        for record in all_partners:
            if record.fiscalcode:
                record.fiscalcode_enc = util.encrypt(record.fiscalcode)
            else:
                record.fiscalcode_enc = None

