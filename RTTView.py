#! python3
import os
import sys
import ctypes
import configparser

from PyQt5 import QtCore, QtGui, QtWidgets, uic
from PyQt5.QtCore import pyqtSlot, pyqtSignal, Qt
from PyQt5.QtWidgets import QApplication, QWidget, QFileDialog
from PyQt5.QtChart import QChart, QChartView, QLineSeries, QLegend

import jlink
import xlink


os.environ['PATH'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'libusb-1.0.24/MinGW64/dll') + os.pathsep + os.environ['PATH']


N_CURVES = 4

class RingBuffer(ctypes.Structure):
    _fields_ = [
        ('sName',        ctypes.c_uint),    # ctypes.POINTER(ctypes.c_char)，64位Python中 ctypes.POINTER 是64位的，与目标芯片不符
        ('pBuffer',      ctypes.c_uint),    # ctypes.POINTER(ctypes.c_byte)
        ('SizeOfBuffer', ctypes.c_uint),
        ('WrOff',        ctypes.c_uint),    # Position of next item to be written. 对于aUp：   芯片更新WrOff，主机更新RdOff
        ('RdOff',        ctypes.c_uint),    # Position of next item to be read.    对于aDown： 主机更新WrOff，芯片更新RdOff
        ('Flags',        ctypes.c_uint),
    ]

class SEGGER_RTT_CB(ctypes.Structure):      # Control Block
    _fields_ = [
        ('acID',              ctypes.c_char * 16),
        ('MaxNumUpBuffers',   ctypes.c_uint),
        ('MaxNumDownBuffers', ctypes.c_uint),
        ('aUp',               RingBuffer * 2),
        ('aDown',             RingBuffer * 2),
    ]


'''
from RTTView_UI import Ui_RTTView
class RTTView(QWidget, Ui_RTTView):
    def __init__(self, parent=None):
        super(RTTView, self).__init__(parent)
        
        self.setupUi(self)
'''
class RTTView(QWidget):
    def __init__(self, parent=None):
        super(RTTView, self).__init__(parent)
        
        uic.loadUi('RTTView.ui', self)

        self.initSetting()

        self.initQwtPlot()

        self.rcvbuff = b''
        
        self.tmrRTT = QtCore.QTimer()
        self.tmrRTT.setInterval(10)
        self.tmrRTT.timeout.connect(self.on_tmrRTT_timeout)
        self.tmrRTT.start()

        self.tmrRTT_Cnt = 0
    
    def initSetting(self):
        if not os.path.exists('setting.ini'):
            open('setting.ini', 'w', encoding='utf-8')
        
        self.conf = configparser.ConfigParser()
        self.conf.read('setting.ini', encoding='utf-8')
        
        if not self.conf.has_section('link'):
            self.conf.add_section('link')
            self.conf.set('link', 'jlink', '')
            self.conf.set('link', 'select', '')
            self.conf.set('link', 'memory', '0x20000000')

            self.conf.add_section('encode')
            self.conf.set('encode', 'input', 'ASCII')
            self.conf.set('encode', 'output', 'ASCII')
            self.conf.set('encode', 'oenter', r'\r\n')  # output enter (line feed)

        self.cmbDLL.addItem(self.conf.get('link', 'jlink'))
        self.daplink_detect()    # add DAPLink

        index = self.cmbDLL.findText(self.conf.get('link', 'select'))
        self.cmbDLL.setCurrentIndex(index if index != -1 else 0)

        self.linRTT.setText(self.conf.get('link', 'memory'))

        self.cmbICode.setCurrentIndex(self.cmbICode.findText(self.conf.get('encode', 'input')))
        self.cmbOCode.setCurrentIndex(self.cmbOCode.findText(self.conf.get('encode', 'output')))
        self.cmbEnter.setCurrentIndex(self.cmbEnter.findText(self.conf.get('encode', 'oenter')))

    def initQwtPlot(self):
        self.PlotData  = [[0]*1000 for i in range(N_CURVES)]
        self.PlotPoint = [[QtCore.QPointF(j, 0) for j in range(1000)] for i in range(N_CURVES)]

        self.PlotChart = QChart()

        self.ChartView = QChartView(self.PlotChart)
        self.ChartView.setVisible(False)
        self.vLayout.insertWidget(0, self.ChartView)
        
        self.PlotCurve = [QLineSeries() for i in range(N_CURVES)]

    def daplink_detect(self):
        try:
            from pyocd.probe import aggregator
            self.daplinks = aggregator.DebugProbeAggregator.get_all_connected_probes()
            if len(self.daplinks) != self.cmbDLL.count() - 1:
                for i in range(1, self.cmbDLL.count()):
                    self.cmbDLL.removeItem(i)
                for i, daplink in enumerate(self.daplinks):
                    self.cmbDLL.addItem(daplink.product_name)
        except Exception as e:
            pass
    
    @pyqtSlot()
    def on_btnOpen_clicked(self):
        if self.btnOpen.text() == '打开连接':
            try:
                if self.cmbDLL.currentIndex() == 0:
                    self.xlk = xlink.XLink(jlink.JLink(self.cmbDLL.currentText(), 'Cortex-M0'))
                
                else:
                    from pyocd.coresight import dap, ap, cortex_m
                    daplink = self.daplinks[self.cmbDLL.currentIndex() - 1]
                    daplink.open()

                    _dp = dap.DebugPort(daplink, None)
                    _dp.init()
                    _dp.power_up_debug()

                    _ap = ap.AHB_AP(_dp, 0)
                    _ap.init()

                    self.xlk = xlink.XLink(cortex_m.CortexM(None, _ap))
                
                addr = int(self.linRTT.text(), 16)
                for i in range(128):
                    data = self.xlk.read_mem_U8(addr + 1024 * i, 1024 + 32) # 多读32字节，防止搜索内容在边界处
                    index = bytes(data).find(b'SEGGER RTT')
                    if index != -1:
                        self.RTTAddr = addr + 1024 * i + index

                        data = self.xlk.read_mem_U8(self.RTTAddr, ctypes.sizeof(SEGGER_RTT_CB))

                        rtt_cb = SEGGER_RTT_CB.from_buffer(bytearray(data))
                        self.aUpAddr = self.RTTAddr + 16 + 4 + 4
                        self.aDownAddr = self.aUpAddr + ctypes.sizeof(RingBuffer) * rtt_cb.MaxNumUpBuffers

                        self.txtMain.append(f'\n_SEGGER_RTT @ 0x{self.RTTAddr:08X} with {rtt_cb.MaxNumUpBuffers} aUp and {rtt_cb.MaxNumDownBuffers} aDown\n')
                        break
                    
                else:
                    raise Exception('Can not find _SEGGER_RTT')

            except Exception as e:
                self.txtMain.append(f'\n{str(e)}\n')

            else:
                self.cmbDLL.setEnabled(False)
                self.btnDLL.setEnabled(False)
                self.linRTT.setEnabled(False)
                self.btnOpen.setText('关闭连接')

        else:
            self.xlk.close()
            self.cmbDLL.setEnabled(True)
            self.btnDLL.setEnabled(True)
            self.linRTT.setEnabled(True)
            self.btnOpen.setText('打开连接')
    
    def aUpRead(self):
        data = self.xlk.read_mem_U8(self.aUpAddr, ctypes.sizeof(RingBuffer))

        aUp = RingBuffer.from_buffer(bytearray(data))
        
        if aUp.RdOff <= aUp.WrOff:
            cnt = aUp.WrOff - aUp.RdOff

        else:
            cnt = aUp.SizeOfBuffer - aUp.RdOff

        if 0 < cnt < 1024*1024:
            data = self.xlk.read_mem_U8(ctypes.cast(aUp.pBuffer, ctypes.c_void_p).value + aUp.RdOff, cnt)
            
            aUp.RdOff = (aUp.RdOff + cnt) % aUp.SizeOfBuffer
            
            self.xlk.write_U32(self.aUpAddr + 4*4, aUp.RdOff)

        else:
            data = []
        
        return bytes(data)

    def aDownWrite(self, bytes):
        data = self.xlk.read_mem_U8(self.aDownAddr, ctypes.sizeof(RingBuffer))

        aDown = RingBuffer.from_buffer(bytearray(data))
        
        if aDown.WrOff >= aDown.RdOff:
            if aDown.RdOff != 0: cnt = min(aDown.SizeOfBuffer - aDown.WrOff, len(bytes))
            else:                cnt = min(aDown.SizeOfBuffer - 1 - aDown.WrOff, len(bytes))   # 写入操作不能使得 aDown.WrOff == aDown.RdOff，以区分满和空
            self.xlk.write_mem(ctypes.cast(aDown.pBuffer, ctypes.c_void_p).value + aDown.WrOff, bytes[:cnt])
            
            aDown.WrOff += cnt
            if aDown.WrOff == aDown.SizeOfBuffer: aDown.WrOff = 0

            bytes = bytes[cnt:]

        if bytes and aDown.RdOff != 0 and aDown.RdOff != 1:        # != 0 确保 aDown.WrOff 折返回 0，!= 1 确保有空间可写入
            cnt = min(aDown.RdOff - 1 - aDown.WrOff, len(bytes))   # - 1 确保写入操作不导致WrOff与RdOff指向同一位置
            self.xlk.write_mem(ctypes.cast(aDown.pBuffer, ctypes.c_void_p).value + aDown.WrOff, bytes[:cnt])

            aDown.WrOff += cnt

        self.xlk.write_U32(self.aDownAddr + 4*3, aDown.WrOff)
    
    def on_tmrRTT_timeout(self):
        self.tmrRTT_Cnt += 1
        if self.btnOpen.text() == '关闭连接':
            try:
                self.rcvbuff += self.aUpRead()
                
                if self.txtMain.isVisible():
                    text = ''
                    if self.cmbICode.currentText() == 'ASCII':
                        text = ''.join([chr(x) for x in self.rcvbuff])
                        self.rcvbuff = b''

                    elif self.cmbICode.currentText() == 'HEX':
                        text = ' '.join([f'{x:02X}' for x in self.rcvbuff]) + ' '
                        self.rcvbuff = b''

                    elif self.cmbICode.currentText() == 'GBK':
                        while len(self.rcvbuff):
                            if self.rcvbuff[0:1].decode('GBK', 'ignore'):
                                text += self.rcvbuff[0:1].decode('GBK')
                                self.rcvbuff = self.rcvbuff[1:]

                            elif len(self.rcvbuff) > 1 and self.rcvbuff[0:2].decode('GBK', 'ignore'):
                                text += self.rcvbuff[0:2].decode('GBK')
                                self.rcvbuff = self.rcvbuff[2:]

                            elif len(self.rcvbuff) > 1:
                                text += chr(self.rcvbuff[0])
                                self.rcvbuff = self.rcvbuff[1:]

                            else:
                                break

                    elif self.cmbICode.currentText() == 'UTF-8':
                        while len(self.rcvbuff):
                            if self.rcvbuff[0:1].decode('UTF-8', 'ignore'):
                                text += self.rcvbuff[0:1].decode('UTF-8')
                                self.rcvbuff = self.rcvbuff[1:]

                            elif len(self.rcvbuff) > 1 and self.rcvbuff[0:2].decode('UTF-8', 'ignore'):
                                text += self.rcvbuff[0:2].decode('UTF-8')
                                self.rcvbuff = self.rcvbuff[2:]

                            elif len(self.rcvbuff) > 2 and self.rcvbuff[0:3].decode('UTF-8', 'ignore'):
                                text += self.rcvbuff[0:3].decode('UTF-8')
                                self.rcvbuff = self.rcvbuff[3:]

                            elif len(self.rcvbuff) > 3 and self.rcvbuff[0:4].decode('UTF-8', 'ignore'):
                                text += self.rcvbuff[0:4].decode('UTF-8')
                                self.rcvbuff = self.rcvbuff[4:]

                            elif len(self.rcvbuff) > 3:
                                text += chr(self.rcvbuff[0])
                                self.rcvbuff = self.rcvbuff[1:]

                            else:
                                break
                    
                    if len(self.txtMain.toPlainText()) > 25000: self.txtMain.clear()
                    self.txtMain.moveCursor(QtGui.QTextCursor.End)
                    self.txtMain.insertPlainText(text)

                else:
                    if self.rcvbuff.rfind(b',') == -1: return
                    
                    d = self.rcvbuff[0:self.rcvbuff.rfind(b',')].split(b',')    # [b'12', b'34'] or [b'12 34', b'56 78']
                    d = [[float(x) for x in X.strip().split()] for X in d]      # [[12], [34]]   or [[12, 34], [56, 78]]
                    for arr in d:
                        for i, x in enumerate(arr):
                            if i == N_CURVES: break

                            self.PlotData[i].pop(0)
                            self.PlotData[i].append(x)
                            self.PlotPoint[i].pop(0)
                            self.PlotPoint[i].append(QtCore.QPointF(999, x))
                    
                    self.rcvbuff = self.rcvbuff[self.rcvbuff.rfind(b',')+1:]

                    if self.tmrRTT_Cnt % 4 == 0:
                        if len(d[-1]) != len(self.PlotChart.series()):
                            for series in self.PlotChart.series():
                                self.PlotChart.removeSeries(series)
                            for i in range(min(len(d[-1]), N_CURVES)):
                                self.PlotCurve[i].setName(f'Curve {i+1}')
                                self.PlotChart.addSeries(self.PlotCurve[i])
                            self.PlotChart.createDefaultAxes()

                        for i in range(len(self.PlotChart.series())):
                            for j, point in enumerate(self.PlotPoint[i]):
                                point.setX(j)
                        
                            self.PlotCurve[i].replace(self.PlotPoint[i])
                    
                        miny = min([min(d) for d in self.PlotData[:len(self.PlotChart.series())]])
                        maxy = max([max(d) for d in self.PlotData[:len(self.PlotChart.series())]])
                        self.PlotChart.axisY().setRange(miny, maxy)
                        self.PlotChart.axisX().setRange(0000, 1000)
            
            except Exception as e:
                self.rcvbuff = b''
                print(str(e))   # 波形显示模式下 txtMain 不可见，因此错误信息不能显示在其上

        else:
            if self.tmrRTT_Cnt % 100 == 1:
                self.daplink_detect()

    @pyqtSlot()
    def on_btnSend_clicked(self):
        if self.btnOpen.text() == '关闭连接':
            text = self.txtSend.toPlainText()

            if self.cmbOCode.currentText() == 'HEX':
                try:
                    self.aDownWrite(bytes([int(x, 16) for x in text.split()]))
                except Exception as e:
                    print(e)

            else:
                if self.cmbEnter.currentText() == r'\r\n':
                    text = text.replace('\n', '\r\n')
                
                try:
                    self.aDownWrite(text.encode(self.cmbOCode.currentText()))
                except Exception as e:
                    print(e)

    @pyqtSlot()
    def on_btnDLL_clicked(self):
        dllpath, filter = QFileDialog.getOpenFileName(caption='JLink_x64.dll路径', filter='动态链接库文件 (*.dll)', directory=self.cmbDLL.itemText(0))
        if dllpath != '':
            self.cmbDLL.setItemText(0, dllpath)

    @pyqtSlot(int)
    def on_chkWave_stateChanged(self, state):
        self.ChartView.setVisible(state == Qt.Checked)
        self.txtMain.setVisible(state == Qt.Unchecked)

    @pyqtSlot()
    def on_btnClear_clicked(self):
        self.txtMain.clear()
    
    def closeEvent(self, evt):
        self.conf.set('link',   'jlink',  self.cmbDLL.itemText(0))
        self.conf.set('link',   'select', self.cmbDLL.currentText())
        self.conf.set('link',   'memory', self.linRTT.text())
        self.conf.set('encode', 'input',  self.cmbICode.currentText())
        self.conf.set('encode', 'output', self.cmbOCode.currentText())
        self.conf.set('encode', 'oenter', self.cmbEnter.currentText())
        self.conf.write(open('setting.ini', 'w', encoding='utf-8'))
        

if __name__ == "__main__":
    app = QApplication(sys.argv)
    rtt = RTTView()
    rtt.show()
    app.exec()
