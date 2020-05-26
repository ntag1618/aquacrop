#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import numpy as np
import pandas
import netCDF4
import datetime as datetime
import calendar as calendar
import sqlite3
from importlib_resources import path

from hm.api import open_hmdataarray

from .utils import read_crop_parameter_from_sqlite
from . import data
from .CarbonDioxide import refconc

import aquacrop_fc


class CropParameters(object):
    def __init__(self, model):
        self.model = model
        self.config = self.model.config.CROP_PARAMETERS
        self.model.nCrop = len(self.model.domain._coords['crop'])
        self.model.CropID = self.config['crop_id']
        self.model.CalendarType = self.config['calendar_type']
        self.model.SwitchGDD = self.config['switch_gdd']
        self.model.GDDmethod = self.config['gdd_method']
        self.load_crop_parameter_database()

    def load_crop_parameter_database(self):
        with path(data, 'crop_parameter_database.sqlite3') as db_path:
            try:
                db_path = db_path.resolve()
            except FileNotFoundError:
                pass
            self.model.CropParameterDatabase = sqlite3.connect(str(db_path))

    def initial(self):        
        self.read()
        self.model.PlantingDateAdj = np.copy(self.model.PlantingDate)
        self.model.HarvestDateAdj = np.copy(self.model.HarvestDate)
        arr_zeros = np.zeros(
            (self.model.nFarm, self.model.nCrop, self.model.domain.nxy)
        )
        self.model.GrowingSeasonIndex = np.copy(arr_zeros.astype(bool))
        self.model.GrowingSeasonDayOne = np.copy(arr_zeros.astype(bool))

        # divide into int/float parameters because this makes it
        # easier when passing as arguments to Fortran extension
        # (where the data type has to be specified)
        int_params_to_compute = [
            'tLinSwitch', 'DAP', 'CanopyDevEndCD', 'CanopyDevEndCD',
            'Canopy10PctCD', 'MaxCanopyCD', 'HIstartCD', 'HIendCD',
            'YldFormCD', 'FloweringCD'
        ]
        flt_params_to_compute = [
            'CC0', 'SxTop', 'SxBot', 'fCO2', 'dHILinear', 'HIGC',
            'CanopyDevEnd', 'Canopy10Pct', 'MaxCanopy', 'HIend',
            'FloweringEnd', 'CurrentConc'
        ]
        for param in int_params_to_compute:
            vars(self.model)[param] = np.copy(arr_zeros.astype(np.int32))
            
        for param in flt_params_to_compute:
            vars(self.model)[param] = np.copy(arr_zeros.astype(np.float64))
            
        self.compute_crop_parameters()

    def read(self):
        self.model.crop_parameters_to_read = [
            'CropType', 'PlantingDate', 'HarvestDate', 'Emergence',
            'MaxRooting',
            'Senescence', 'Maturity', 'HIstart', 'Flowering', 'YldForm',
            'PolHeatStress', 'PolColdStress', 'BioTempStress', 'PlantPop',
            'Determinant', 'ETadj', 'LagAer', 'Tbase', 'Tupp', 'Tmax_up',
            'Tmax_lo', 'Tmin_up', 'Tmin_lo', 'GDD_up', 'GDD_lo', 'fshape_b',
            'PctZmin', 'Zmin', 'Zmax', 'fshape_r', 'fshape_ex', 'SxTopQ',
            'SxBotQ', 'a_Tr', 'SeedSize', 'CCmin', 'CCx', 'CDC', 'CGC',
            'Kcb', 'fage', 'WP', 'WPy', 'fsink', 'bsted', 'bface', 'HI0',
            'HIini', 'dHI_pre', 'a_HI', 'b_HI', 'dHI0', 'exc', 'MaxFlowPct',
            'p_up1', 'p_up2', 'p_up3', 'p_up4', 'p_lo1', 'p_lo2', 'p_lo3',
            'p_lo4', 'fshape_w1', 'fshape_w2', 'fshape_w3', 'fshape_w4',
            'Aer', 'beta', 'GermThr'
        ]
        
        if len(self.model.crop_parameters_to_read) > 0:
            for param in self.model.crop_parameters_to_read:
                
                # 1 - Try to read from configuration file:
                if param in self.config.keys():
                    parameter_values = np.array(self.config[param])
                    if (len(parameter_values) == 1) | (len(parameter_values) == self.model.nCrop):                        
                        vars(self.model)[param] = np.broadcast_to(
                            parameter_values[:, None, None],
                            (self.model.nFarm, self.model.nCrop,
                             self.model.domain.nxy)
                        )
                        
                    else:
                        raise ValueError(
                            "Error reading parameter " + param
                            + " from configuration file: length"
                            + " of parameter list must equal number"
                            + " of crops in simulation"
                        )
                        
                else:        
                    # 2 - Try to read from netCDF file
                    try:
                        arr = open_hmdataarray(
                            self.model.config.CROP_PARAMETERS['filename'],
                            param,
                            self.model.domain,
                            self.model.config.CROP_PARAMETERS['is_1d'],
                            self.model.config.CROP_PARAMETERS['xy_dimname'],
                        )
                        vars(self.model)[param] = np.broadcast_to(
                            arr.values,
                            (self.model.nFarm, self.model.nCrop, self.model.domain.nxy)
                        )
                        
                    # 3 - Read from default parameter database
                    except:                        
                        try:
                            parameter_values = np.zeros((self.model.nCrop))
                            for index, crop_id in enumerate(self.model.CropID):
                                parameter_values[index] = read_crop_parameter_from_sqlite(
                                    self.model.CropParameterDatabase,
                                    crop_id,
                                    param
                                )[0]
                            vars(self.model)[param] = np.broadcast_to(
                                parameter_values[:, None, None],
                                (self.model.nFarm, self.model.nCrop,
                                 self.model.domain.nxy)
                            )

                        except:
                            
                            raise KeyError(
                                "Error reading parameter "
                                + param + " from crop parameter database"
                            )

    def compute_crop_parameters(self):
        self.compute_initial_canopy_cover()
        self.compute_root_extraction_terms()
        self.adjust_planting_and_harvesting_date()
        self.compute_canopy_dev_end()
        self.compute_canopy_10pct()
        self.compute_max_canopy()
        self.compute_hi_end()
        self.compute_flowering_end_cd()
        self.compute_crop_calendar()
        self.compute_HIGC()
        self.compute_HI_linear()

    def compute_initial_canopy_cover(self):
        aquacrop_fc.crop_parameters_w.compute_init_cc_w(
            self.model.CC0.T,
            self.model.PlantPop.T,
            self.model.SeedSize.T,
            self.model.nFarm, self.model.nCrop, self.model.domain.nxy
        )

    def compute_root_extraction_terms(self):
        aquacrop_fc.crop_parameters_w.compute_root_extraction_terms_w(
            self.model.SxTop.T,
            self.model.SxBot.T,
            self.model.SxTopQ.T,
            self.model.SxBotQ.T,
            self.model.nFarm, self.model.nCrop, self.model.domain.nxy
        )

    def adjust_planting_and_harvesting_date(self):
        """Adjust planting and harvest date to account for 
        leap year.
        """
        leap_year = calendar.isleap(self.model.time.curr_time.year)
        aquacrop_fc.crop_parameters_w.adjust_pd_hd_w(
            np.int32(self.model.PlantingDateAdj).T,
            np.int32(self.model.HarvestDateAdj).T,
            np.int32(self.model.PlantingDate).T,
            np.int32(self.model.HarvestDate).T,
            np.int32(self.model.time.doy),
            np.int32(self.model.time.timestep),
            np.int32(leap_year),
            self.model.nFarm, self.model.nCrop, self.model.domain.nxy
        )

    def compute_canopy_dev_end(self):
        """Calculate time from sowing to end of vegetative
        growth period.
        """
        aquacrop_fc.crop_parameters_w.compute_canopy_dev_end_w(
            self.model.CanopyDevEnd.T,
            self.model.Senescence.T,
            self.model.HIstart.T,
            self.model.Flowering.T,
            self.model.Determinant,
            self.model.nFarm, self.model.nCrop, self.model.domain.nxy            
            )
        
    def compute_canopy_10pct(self):
        """Calculate time from sowing to 10% canopy cover
        (non-stressed conditions).
        """
        aquacrop_fc.crop_parameters_w.compute_canopy_10pct_w(
            self.model.Canopy10Pct.T,
            self.model.Emergence.T,
            self.model.CC0.T,
            self.model.CGC.T,
            self.model.nFarm, self.model.nCrop, self.model.domain.nxy
        )
            
    def compute_max_canopy(self):
        """Calculate time from sowing to maximum canopy 
        cover (non-stressed conditions).
        """
        aquacrop_fc.crop_parameters_w.compute_max_canopy_w(
            self.model.MaxCanopy.T,
            self.model.Emergence.T,
            self.model.CCx.T,
            self.model.CC0.T,
            self.model.CGC.T,
            self.model.nFarm, self.model.nCrop, self.model.domain.nxy
        )
            
    def compute_hi_end(self):
        """Calculate time from sowing to end of yield
        formation.
        """
        aquacrop_fc.crop_parameters_w.compute_hi_end_w(
            self.model.HIend.T,
            self.model.HIstart.T,
            self.model.YldForm.T,
            self.model.nFarm, self.model.nCrop, self.model.domain.nxy
        )

    def compute_flowering_end_cd(self):
        aquacrop_fc.crop_parameters_w.compute_flowering_end_cd_w(
            self.model.FloweringEnd.T,
            np.int32(self.model.FloweringCD).T,
            np.int32(self.model.Flowering).T,
            self.model.HIstart.T,
            self.model.CropType.T,
            self.model.nFarm, self.model.nCrop, self.model.domain.nxy
        )        
            
    def compute_HIGC(self):
        aquacrop_fc.crop_parameters_w.compute_higc_w(
            self.model.HIGC.T,
            self.model.YldFormCD.T,
            self.model.HI0.T,
            self.model.HIini.T,
            self.model.nFarm,
            self.model.nCrop,
            self.model.domain.nxy
        )

    def compute_HI_linear(self):
        aquacrop_fc.crop_parameters_w.compute_hi_linear_w(
            self.model.tLinSwitch.T,
            self.model.dHILinear.T,
            self.model.HIini.T,
            self.model.HI0.T,
            self.model.HIGC.T,
            self.model.YldFormCD.T,
            self.model.nFarm,
            self.model.nCrop,
            self.model.domain.nxy
        )

    def compute_pd_hd(self):
        """Adjust harvest date so that it always falls 
        after the planting date.
        """
        pd = np.copy(self.model.PlantingDateAdj)
        hd = np.copy(self.model.HarvestDateAdj)
        hd[hd < pd] += 365
        sd = self.model.time.curr_time.timetuple().tm_yday
        planting_day_before_start_day = sd > pd
        pd[planting_day_before_start_day] = 0  # is this possible?
        hd[planting_day_before_start_day] = 0
        return pd, hd

    def compute_day_index(self, pd, hd):
        sd = self.model.time.curr_time.timetuple().tm_yday
        max_harvest_date = int(np.max(hd))
        day_index = np.arange(sd, max_harvest_date + 1)
        day_index = (
            day_index[:, None, None, None] *
            np.ones(
                (self.model.nFarm,
                 self.model.nCrop,
                 self.model.domain.nxy)
            )[None, ...]
        )
        return day_index

    def compute_growing_season_index(self, day_idx, pd, hd):
        growing_season_index = ((day_idx >= pd) & (day_idx <= hd))
        return growing_season_index

    def compute_cumulative_gdd(self, hd, growing_season_index):
        sd = self.model.time.curr_time.timetuple().tm_yday
        max_harvest_date = int(np.max(hd))
        start_time = self.model.time.curr_time
        time_slc = slice(
            self.model.time.curr_time,
            self.model.time.curr_time +
            datetime.timedelta(int(max_harvest_date - sd))
        )
        print(self.model.model.tmin)
        self.model.model.tmin.select(time_slc)
        self.model.model.tmax.select(time_slc)
        tmin = (
            self.model.model.tmin.values[:, None, None, ...]
            * np.ones_like(growing_season_index)
        )
        tmax = (
            self.model.model.tmax.values[:, None, None, ...]
            * np.ones_like(growing_season_index)
        )
        self.model.model.tmin.select(time=self.model.time.curr_time)
        self.model.model.tmax.select(time=self.model.time.curr_time)

        # calculate GDD according to the various methods
        if self.model.GDDmethod == 1:
            tmean = ((tmax + tmin) / 2)
            tmean = np.clip(tmean, self.model.Tbase, self.model.Tupp)
        elif self.model.GDDmethod == 2:
            tmax = np.clip(tmax, self.model.Tbase, self.model.Tupp)
            tmin = np.clip(tmin, self.model.Tbase, self.model.Tupp)
            tmean = ((tmax + tmin) / 2)
        elif self.model.GDDmethod == 3:
            tmax = np.clip(tmax, self.model.Tbase, self.model.Tupp)
            tmin = np.clip(tmin, None, self.model.Tupp)
            tmean = ((tmax + tmin) / 2)
            tmean = np.clip(tmean, self.model.Tbase, None)

        tbase = np.broadcast_to(self.model.Tbase, tmin.shape).copy()
        tbase *= growing_season_index
        tmean *= growing_season_index
        GDD = (tmean - tbase)
        GDDcum = np.cumsum(GDD, axis=0)
        return GDDcum

    def compute_crop_calendar_type_1(self):
        EmergenceCD = np.copy(self.model.Emergence)
        Canopy10PctCD = np.copy(self.model.Canopy10Pct)
        MaxRootingCD = np.copy(self.model.MaxRooting)
        SenescenceCD = np.copy(self.model.Senescence)
        MaturityCD = np.copy(self.model.Maturity)
        self.model.MaxCanopyCD = np.copy(self.model.MaxCanopy)
        self.model.CanopyDevEndCD = np.copy(self.model.CanopyDevEnd)
        self.model.HIstartCD = np.copy(self.model.HIstart)
        self.model.HIendCD = np.copy(self.model.HIend)
        self.model.YldFormCD = np.copy(self.model.YldForm)
        FloweringEndCD = np.copy(self.model.FloweringEnd)
        self.model.FloweringCD = np.copy(self.model.Flowering)

        if self.model.SwitchGDD:
            pd, hd = self.compute_pd_hd()
            day_idx = self.compute_day_index(pd, hd)
            growing_season_idx = self.compute_growing_season_index(
                day_idx, pd, hd)
            GDDcum = self.compute_cumulative_gdd(hd, growing_season_idx)
            if (self.model.CalendarType == 1) & (self.model.SwitchGDD):
                # Find GDD equivalent for each crop calendar variable
                m, n, p = pd.shape
                I, J, K = np.ogrid[:m, :n, :p]
                emergence_idx = np.int32(pd + EmergenceCD)
                self.model.Emergence = GDDcum[emergence_idx, I, J, K]
                canopy10pct_idx = np.int32(pd + Canopy10PctCD)
                self.model.Canopy10Pct = GDDcum[canopy10pct_idx, I, J, K]
                maxrooting_idx = np.int32(pd + MaxRootingCD)
                self.model.MaxRooting = GDDcum[maxrooting_idx, I, J, K]
                maxcanopy_idx = np.int32(pd + self.model.MaxCanopyCD)
                self.model.MaxCanopy = GDDcum[maxcanopy_idx, I, J, K]
                canopydevend_idx = np.int32(pd + self.model.CanopyDevEndCD)
                self.model.CanopyDevEnd = GDDcum[canopydevend_idx, I, J, K]
                senescence_idx = np.int32(pd + SenescenceCD)
                self.model.Senescence = GDDcum[senescence_idx, I, J, K]
                maturity_idx = np.int32(pd + MaturityCD)
                self.model.Maturity = GDDcum[maturity_idx, I, J, K]
                histart_idx = np.int32(pd + self.model.HIstartCD)
                self.model.HIstart = GDDcum[histart_idx, I, J, K]
                hiend_idx = np.int32(pd + self.model.HIendCD)
                self.model.HIend = GDDcum[hiend_idx, I, J, K]
                yldform_idx = np.int32(pd + self.model.YldFormCD)
                self.model.YldForm = GDDcum[yldform_idx, I, J, K]

                cond2 = (self.model.CropType == 3)
                floweringend_idx = np.int32(pd + FloweringEndCD)
                self.model.FloweringEnd[cond2] = GDDcum[floweringend_idx,
                                                        I, J, K][cond2]
                self.model.Flowering[cond2] = (
                    self.model.FloweringEnd - self.model.HIstart)[cond2]

                # Convert CGC to GDD mode
                self.model.CGC = (np.log((((0.98 * self.model.CCx) - self.model.CCx) * self.model.CC0) /
                                         (-0.25 * (self.model.CCx ** 2)))) / (-(self.model.MaxCanopy - self.model.Emergence))

                # Convert CDC to GDD mode
                tCD = MaturityCD - SenescenceCD
                tCD[tCD <= 0] = 1
                tGDD = self.model.Maturity - self.model.Senescence
                tGDD[tGDD <= 0] = 5
                self.model.CDC = (self.model.CCx / tGDD) * np.log(1 +
                                                                  ((1 - self.model.CCi / self.model.CCx) / 0.05))

                # Set calendar type to GDD mode
                self.model._configuration.CROP_PARAMETERS['calendar_type'] = "2"

    def compute_crop_calendar_type_2(self, update=False):
        pd, hd = self.compute_pd_hd()
        day_idx = self.compute_day_index(pd, hd)
        growing_season_idx = self.compute_growing_season_index(day_idx, pd, hd)
        GDDcum = self.compute_cumulative_gdd(hd, growing_season_idx)

        maxcanopy_idx = np.copy(day_idx)
        maxcanopy_idx[np.logical_not(GDDcum > self.model.MaxCanopy)] = 999
        maxcanopy_idx = np.nanmin(maxcanopy_idx, axis=0)

        canopydevend_idx = np.copy(day_idx)
        canopydevend_idx[np.logical_not(
            GDDcum > self.model.CanopyDevEnd)] = 999
        canopydevend_idx = np.nanmin(canopydevend_idx, axis=0)

        histart_idx = np.copy(day_idx)
        histart_idx[np.logical_not(GDDcum > self.model.HIstart)] = 999
        histart_idx = np.nanmin(histart_idx, axis=0)

        hiend_idx = np.copy(day_idx)
        hiend_idx[np.logical_not(GDDcum > self.model.HIend)] = 999
        hiend_idx = np.nanmin(hiend_idx, axis=0)

        floweringend_idx = np.copy(day_idx)
        floweringend_idx[np.logical_not(
            GDDcum > self.model.FloweringEnd)] = 999
        floweringend_idx = np.nanmin(floweringend_idx, axis=0)

        if update:
            maxcanopycd = maxcanopy_idx - pd + 1
            self.model.MaxCanopyCD[self.model.GrowingSeasonDayOne] = maxcanopycd[self.model.GrowingSeasonDayOne]
            canopydevendcd = canopydevend_idx - pd + 1
            self.model.CanopyDevEndCD[self.model.GrowingSeasonDayOne] = canopydevendcd[self.model.GrowingSeasonDayOne]
            histartcd = histart_idx - pd + 1
            self.model.HIstartCD[self.model.GrowingSeasonDayOne] = histartcd[self.model.GrowingSeasonDayOne]
            hiendcd = hiend_idx - pd + 1
            self.model.HIendCD[self.model.GrowingSeasonDayOne] = hiendcd[self.model.GrowingSeasonDayOne]
            floweringendcd = (floweringend_idx - pd + 1) - self.model.HIstartCD
            cond1 = (self.model.CropType == 3) & (
                self.model.GrowingSeasonDayOne)
            self.model.FloweringCD[cond1] = floweringendcd[cond1]
            yldformcd = self.model.HIendCD - self.model.HIstartCD
            self.model.YldFormCD[self.model.GrowingSeasonDayOne] = yldformcd[self.model.GrowingSeasonDayOne]

        else:
            self.model.MaxCanopyCD = maxcanopy_idx - pd + 1
            self.model.CanopyDevEndCD = canopydevend_idx - pd + 1
            self.model.HIstartCD = histart_idx - pd + 1
            self.model.HIendCD = hiend_idx - pd + 1
            cond1 = (self.model.CropType == 3)
            floweringendcd = (floweringend_idx - pd + 1) - self.model.HIstartCD
            self.model.FloweringCD[cond1] = floweringendcd[cond1]
            self.model.YldFormCD = self.model.HIendCD - self.model.HIstartCD

    def compute_crop_calendar(self):
        if self.model.CalendarType == 1:
            self.compute_crop_calendar_type_1()
        elif self.model.CalendarType == 2:
            self.compute_crop_calendar_type_2()

    def update_crop_parameters(self):
        if (self.model.CalendarType == 2):
            if (np.any(self.model.GrowingSeasonDayOne)):
                self.compute_crop_calendar_type_2(update=True)
                self.compute_HIGC()
                self.compute_HI_linear()

    def compute_water_productivity_adjustment_factor(self):
        """Function to calculate water productivity adjustment factor 
        for elevation in CO2 concentration"""

        # Get CO2 weighting factor
        fw = np.zeros_like(self.model.conc.values)
        cond1 = (self.model.conc.values > refconc)  # self.model.RefConc)
        cond11 = (cond1 & (self.model.conc.values >= 550))
        fw[cond11] = 1
        cond12 = (cond1 & np.logical_not(cond11))
        # self.model.RefConc)))[cond12]
        fw[cond12] = (
            1 - ((550 - self.model.conc.values) / (550 - refconc)))[cond12]

        # Determine adjustment for each crop in first year of simulation
        fCO2 = ((self.model.conc.values / refconc) /  # self.model.RefConc) /
                (1 + (self.model.conc.values - refconc) * ((1 - fw)  # self.model.RefConc) * ((1 - fw)
                                                           * self.model.bsted + fw
                                                           * ((self.model.bsted * self.model.fsink)
                                                              + (self.model.bface
                                                                 * (1 - self.model.fsink))))))

        # Consider crop type
        ftype = (40. - self.model.WP) / (40. - 20)
        ftype = np.clip(ftype, 0, 1)
        fCO2 = 1 + ftype * (fCO2 - 1)

        self.model.fCO2[self.model.GrowingSeasonDayOne] = fCO2[self.model.GrowingSeasonDayOne]
        conc = np.broadcast_to(
            self.model.conc.values, (self.model.nFarm, self.model.nCrop, self.model.domain.nxy))
        # conc = (self.model.conc[None,:] * np.ones((self.model.nCrop))[:,None])
        self.model.CurrentConc[self.model.GrowingSeasonDayOne] = conc[self.model.GrowingSeasonDayOne]

    def dynamic(self):
        """Function to update parameters for current crop grown as well 
        as counters pertaining to crop growth
        """
        # Update crop parameters for currently grown crops
        self.adjust_planting_and_harvesting_date()
        self.update_growing_season()
        self.compute_water_productivity_adjustment_factor()  # TODO
        self.update_crop_parameters()

    def update_growing_season(self):
        gs = np.int32(self.model.GrowingSeasonIndex.copy())
        gsd = np.int32(self.model.GrowingSeasonDayOne.copy())
        endnum = netCDF4.date2num(
            datetime.datetime(
                self.model.time.endtime.year,
                self.model.time.endtime.month,
                self.model.time.endtime.day
            ),
            units='days since 1900-01-01 00:00:00'
        )
        startnum = netCDF4.date2num(
            datetime.datetime(
                self.model.time.year, 1, 1
            ),
            units='days since 1900-01-01 00:00:00'
        )
        aquacrop_fc.crop_parameters_w.update_growing_season_w(
            gs.T,
            gsd.T,
            # np.int32(self.model.GrowingSeasonIndex).T,
            # np.int32(self.model.GrowingSeasonDayOne).T,
            np.int32(self.model.DAP).T,
            np.int32(self.model.PlantingDateAdj).T,
            np.int32(self.model.HarvestDateAdj).T,
            np.int32(self.model.CropDead).T,
            np.int32(self.model.CropMature).T,
            self.model.time.doy,
            self.model.time.timestep,
            startnum,
            endnum,
            # self.model.time.currentYearStartNum,
            # self.model.time.endTimeNum,
            self.model.nFarm,
            self.model.nCrop,
            self.model.domain.nxy
        )
        self.model.GrowingSeasonIndex = gs.astype(bool)
        self.model.GrowingSeasonDayOne = gsd.astype(bool)
